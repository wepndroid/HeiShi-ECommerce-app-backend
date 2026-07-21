import hashlib
import io
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi import HTTPException
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.auth import revoke_user_refresh_tokens
from app.media_processing import MediaValidationError, process_image_variants
from app.models import (
    AdminSupportMessage,
    AnonymousSession,
    Conversation,
    DeviceSession,
    ExposureRule,
    Listing,
    MediaAsset,
    Message,
    NotificationDispatch,
    NotificationPreference,
    Order,
    PrivateOffer,
    RefreshToken,
    ShareAttributionEvent,
    ShareRecord,
    SystemNotification,
    User,
    UserSettings,
)
from app.notification_jobs import (
    _process_expired_private_offers,
    _process_order_reminders,
    enqueue_notification,
)
from app.payments.fulfillment import fulfill_paid_order
from app.config import settings
from app.routers.auth import _issue_tokens
from app.routers.listings import create_listing
from app.schemas import CreateListingRequest
from app.routers.platform_features import (
    AnonymousConsentRequest,
    CreateOfferRequest,
    MediaModerationRequest,
    PendingActionRequest,
    PreferenceUpdate,
    ShareEventRequest,
    SupportConversationRequest,
    _existing_duplicate_asset,
    accept_private_offer,
    admin_moderate_media_asset,
    consume_pending_action,
    create_pending_action,
    create_private_offer,
    get_private_offer,
    create_support_conversation,
    deactivate_exposure_rule,
    get_private_offer,
    link_anonymous_session,
    record_share_event,
    resolve_share_link,
    restore_normal_exposure,
    update_anonymous_session_consent,
    update_notification_preference,
)


class PlatformFeatureTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.seller = User(
            id="seller",
            nickname="Seller",
            password_hash="test",
            heishi_id="HSSELLER",
        )
        self.buyer = User(
            id="buyer",
            nickname="Buyer",
            password_hash="test",
            heishi_id="HSBUYER",
        )
        self.stranger = User(
            id="stranger",
            nickname="Stranger",
            password_hash="test",
            heishi_id="HSSTRANGER",
        )
        self.db.add_all((self.seller, self.buyer, self.stranger))
        self.db.flush()
        self.listing = Listing(
            seller_id=self.seller.id,
            title="Negotiable item",
            description="test",
            price=100,
            category_key="home",
            location_label="Melbourne",
            image_url="https://example.com/item.jpg",
            status="active",
            review_status="approved",
            negotiable=True,
        )
        self.db.add(self.listing)
        self.db.flush()
        self.conversation = Conversation(
            listing_id=self.listing.id,
            buyer_id=self.buyer.id,
            seller_id=self.seller.id,
            last_message_text="",
        )
        self.db.add(self.conversation)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_private_offer_is_buyer_specific_and_acceptance_is_idempotent(self):
        offer = create_private_offer(
            self.conversation.id,
            CreateOfferRequest(
                negotiatedPrice=80,
                quantity=1,
                shippingFee=5,
                expiresInMinutes=60,
            ),
            self.seller,
            self.db,
        )
        with self.assertRaises(HTTPException) as forbidden:
            accept_private_offer(offer["id"], self.stranger, self.db)
        self.assertEqual(forbidden.exception.status_code, 403)

        accepted = accept_private_offer(offer["id"], self.buyer, self.db)
        repeated = accept_private_offer(offer["id"], self.buyer, self.db)
        self.assertFalse(accepted["idempotent"])
        self.assertTrue(repeated["idempotent"])
        self.assertEqual(accepted["orderId"], repeated["orderId"])
        order = self.db.query(Order).filter(Order.id == accepted["orderId"]).one()
        self.assertEqual(order.amount, 85)
        self.assertEqual(order.delivery_method, "express")
        self.assertEqual(order.private_offer_id, offer["id"])
        self.assertEqual(self.listing.price, 100)

    def test_private_offer_becomes_viewed_only_for_designated_buyer(self):
        offer = create_private_offer(
            self.conversation.id,
            CreateOfferRequest(negotiatedPrice=80, expiresInMinutes=60),
            self.seller,
            self.db,
        )
        with self.assertRaises(HTTPException):
            get_private_offer(offer["id"], self.stranger, self.db)
        viewed = get_private_offer(offer["id"], self.buyer, self.db)
        self.assertEqual("VIEWED", viewed["status"])

    def test_paid_private_offer_invalidates_other_buyer_offers(self):
        other_buyer = User(
            id="other-buyer",
            nickname="Other buyer",
            password_hash="test",
            heishi_id="HSOTHERBUYER",
        )
        self.db.add(other_buyer)
        self.db.flush()
        other_conversation = Conversation(
            listing_id=self.listing.id,
            buyer_id=other_buyer.id,
            seller_id=self.seller.id,
            last_message_text="",
        )
        self.db.add(other_conversation)
        self.db.commit()
        accepted_offer = create_private_offer(
            self.conversation.id,
            CreateOfferRequest(negotiatedPrice=80),
            self.seller,
            self.db,
        )
        other_offer = create_private_offer(
            other_conversation.id,
            CreateOfferRequest(negotiatedPrice=85),
            self.seller,
            self.db,
        )
        accepted = accept_private_offer(accepted_offer["id"], self.buyer, self.db)
        order = self.db.query(Order).filter(Order.id == accepted["orderId"]).one()
        fulfill_paid_order(self.db, order)
        refreshed_other = get_private_offer(other_offer["id"], other_buyer, self.db)
        self.assertEqual(refreshed_other["status"], "INVALIDATED")
        other_message = (
            self.db.query(Message)
            .filter(
                Message.conversation_id == other_conversation.id,
                Message.message_type == "private_offer",
            )
            .one()
        )
        self.assertEqual(
            json.loads(other_message.structured_payload_json)["status"],
            "INVALIDATED",
        )

    def test_private_offer_can_differ_from_public_price_in_either_direction(self):
        offer = create_private_offer(
            self.conversation.id,
            CreateOfferRequest(negotiatedPrice=125, shippingFee=5),
            self.seller,
            self.db,
        )
        self.assertEqual(offer["negotiatedPrice"], 125)
        self.assertEqual(offer["totalAmount"], 130)

    def test_pending_action_rejects_unsafe_redirect_paths(self):
        for return_path in (
            "https://example.com/steal",
            "//example.com/steal",
            "/\\example.com/steal",
        ):
            with self.subTest(return_path=return_path):
                with self.assertRaises(HTTPException) as invalid:
                    create_pending_action(
                        PendingActionRequest(
                            actionType="purchase",
                            returnPath=return_path,
                        ),
                        self.db,
                    )
                self.assertEqual(422, invalid.exception.status_code)

    def test_offer_requires_negotiable_single_item_listing(self):
        self.listing.negotiable = False
        self.db.commit()
        with self.assertRaises(HTTPException) as disabled:
            create_private_offer(
                self.conversation.id,
                CreateOfferRequest(negotiatedPrice=90),
                self.seller,
                self.db,
            )
        self.assertEqual(disabled.exception.status_code, 409)
        self.listing.negotiable = True
        self.db.commit()
        with self.assertRaises(HTTPException) as quantity:
            create_private_offer(
                self.conversation.id,
                CreateOfferRequest(negotiatedPrice=90, quantity=2),
                self.seller,
                self.db,
            )
        self.assertEqual(quantity.exception.status_code, 422)

    def test_mandatory_payment_notification_cannot_be_disabled(self):
        with self.assertRaises(HTTPException) as mandatory:
            update_notification_preference(
                PreferenceUpdate(
                    userRoleContext="buyer",
                    category="payment_update",
                    inAppEnabled=False,
                    pushEnabled=False,
                ),
                self.buyer,
                self.db,
            )
        self.assertEqual(mandatory.exception.status_code, 409)

    def test_unknown_notification_category_is_rejected(self):
        with self.assertRaises(HTTPException) as invalid:
            update_notification_preference(
                PreferenceUpdate(
                    userRoleContext="buyer",
                    category="invented_category",
                    inAppEnabled=True,
                    pushEnabled=True,
                ),
                self.buyer,
                self.db,
            )
        self.assertEqual(invalid.exception.status_code, 422)
        self.assertEqual(
            invalid.exception.detail["code"],
            "INVALID_NOTIFICATION_CATEGORY",
        )

    def test_order_support_message_uses_resolved_transaction_role(self):
        admin = User(
            id="admin",
            nickname="Admin",
            password_hash="test",
            heishi_id="HSADMIN",
            is_admin=True,
            account_status="normal",
        )
        order = Order(
            listing_id=self.listing.id,
            buyer_id=self.buyer.id,
            seller_id=self.seller.id,
            amount=self.listing.price,
            status="pendingPay",
        )
        self.db.add_all([admin, order])
        self.db.commit()

        result = create_support_conversation(
            SupportConversationRequest(
                userRoleContext="both",
                orderId=order.id,
                subject="Order help",
                body="Please help with this order.",
            ),
            self.buyer,
            self.db,
        )

        self.assertEqual(result["userRoleContext"], "buyer")
        message = self.db.query(AdminSupportMessage).one()
        self.assertEqual(message.sender_role, "buyer")

    def test_deactivating_exposure_rule_notifies_seller_of_restoration(self):
        admin = User(
            id="exposure-admin",
            nickname="Exposure Admin",
            password_hash="test",
            heishi_id="HSEXPOSUREADMIN",
            is_admin=True,
            account_status="normal",
        )
        rule = ExposureRule(
            product_id=self.listing.id,
            rule_type="boost",
            exposure_weight=3,
            created_by=admin.id,
        )
        self.db.add_all([admin, rule])
        self.db.commit()

        result = deactivate_exposure_rule(rule.id, admin, self.db)

        self.assertEqual(result["status"], "inactive")
        notification = (
            self.db.query(SystemNotification)
            .filter(
                SystemNotification.user_id == self.seller.id,
                SystemNotification.notification_type == "listing_exposure_restored",
            )
            .one()
        )
        self.assertEqual(notification.business_id, str(self.listing.id))

    def test_restore_normal_exposure_deactivates_all_manual_rules(self):
        admin = User(
            id="restore-admin",
            nickname="Restore Admin",
            password_hash="test",
            heishi_id="HSRESTOREADMIN",
            is_admin=True,
            account_status="normal",
        )
        rules = [
            ExposureRule(
                product_id=self.listing.id,
                rule_type="boost",
                exposure_weight=2,
                created_by=admin.id,
            ),
            ExposureRule(
                product_id=self.listing.id,
                rule_type="pin",
                exposure_weight=3,
                created_by=admin.id,
            ),
        ]
        self.db.add_all([admin, *rules])
        self.db.commit()

        result = restore_normal_exposure(self.listing.id, admin, self.db)

        self.assertEqual(result["deactivatedRuleCount"], 2)
        self.assertEqual(
            self.db.query(ExposureRule)
            .filter(
                ExposureRule.product_id == self.listing.id,
                ExposureRule.status == "active",
            )
            .count(),
            0,
        )
        restored = (
            self.db.query(SystemNotification)
            .filter(
                SystemNotification.user_id == self.seller.id,
                SystemNotification.notification_type == "listing_exposure_restored",
            )
            .one()
        )
        self.assertEqual(restored.business_id, str(self.listing.id))

    def test_push_only_notification_keeps_independent_delivery_payload(self):
        self.db.add(
            NotificationPreference(
                user_id=self.buyer.id,
                user_role_context="buyer",
                category="marketing",
                in_app_enabled=False,
                push_enabled=True,
            )
        )
        self.db.commit()
        created = enqueue_notification(
            self.db,
            user_id=self.buyer.id,
            role="buyer",
            category="marketing",
            notification_type="test",
            title="English",
            body="Body",
            title_zh="中文",
            body_zh="内容",
            business_type="listing",
            business_id=str(self.listing.id),
            deep_link=f"heymarket://listing/{self.listing.id}",
            deduplication_key="test:push-only",
        )
        self.db.commit()
        dispatch = self.db.query(NotificationDispatch).one()
        self.assertTrue(created)
        self.assertIsNone(dispatch.notification_id)
        self.assertEqual(dispatch.channel, "push")
        self.assertIn('"businessType": "listing"', dispatch.payload_json)

    def test_push_and_sms_preferences_create_independent_dispatches(self):
        self.db.add(
            NotificationPreference(
                user_id=self.buyer.id,
                user_role_context="buyer",
                category="marketing",
                in_app_enabled=True,
                push_enabled=True,
                sms_enabled=True,
            )
        )
        self.db.commit()
        enqueue_notification(
            self.db,
            user_id=self.buyer.id,
            role="buyer",
            category="marketing",
            notification_type="test",
            title="English",
            body="Body",
            title_zh="中文",
            body_zh="内容",
            business_type="listing",
            business_id=str(self.listing.id),
            deep_link=f"heymarket://listing/{self.listing.id}",
            deduplication_key="test:multi-channel",
        )
        self.db.commit()
        rows = (
            self.db.query(NotificationDispatch)
            .order_by(NotificationDispatch.channel.asc())
            .all()
        )
        self.assertEqual([row.channel for row in rows], ["push", "sms"])
        self.assertEqual(len({row.deduplication_key for row in rows}), 2)

    def test_role_specific_notification_preference_overrides_both(self):
        self.db.add_all(
            [
                NotificationPreference(
                    user_id=self.buyer.id,
                    user_role_context="both",
                    category="marketing",
                    in_app_enabled=True,
                    push_enabled=True,
                ),
                NotificationPreference(
                    user_id=self.buyer.id,
                    user_role_context="buyer",
                    category="marketing",
                    in_app_enabled=False,
                    push_enabled=False,
                ),
            ]
        )
        self.db.commit()
        enqueue_notification(
            self.db,
            user_id=self.buyer.id,
            role="buyer",
            category="marketing",
            notification_type="test",
            title="English",
            body="Body",
            title_zh="中文",
            body_zh="内容",
            business_type="listing",
            business_id=str(self.listing.id),
            deep_link=f"heymarket://listing/{self.listing.id}",
            deduplication_key="test:role-override",
        )
        self.db.commit()
        self.assertEqual(self.db.query(SystemNotification).count(), 0)
        dispatch = self.db.query(NotificationDispatch).one()
        self.assertEqual(dispatch.channel, "in_app")
        self.assertEqual(dispatch.status, "disabled")

    def test_image_processing_corrects_and_generates_required_variants(self):
        source = Image.new("RGB", (2400, 1600), color=(12, 80, 120))
        buffer = io.BytesIO()
        source.save(buffer, format="JPEG", quality=95)
        content = buffer.getvalue()
        processed = process_image_variants(content)
        self.assertEqual(processed.width, 2400)
        self.assertEqual(processed.height, 1600)
        self.assertEqual(
            set(processed.variants),
            {"thumbnail", "preview", "fullscreen", "adminReview"},
        )
        self.assertEqual(len(hashlib.sha256(processed.original).hexdigest()), 64)

    def test_corrupt_image_is_rejected(self):
        with self.assertRaises(MediaValidationError):
            process_image_variants(b"not-an-image")

    def test_pending_action_is_bound_to_authenticated_user_and_idempotent(self):
        anonymous = AnonymousSession()
        self.db.add(anonymous)
        self.db.commit()
        pending = create_pending_action(
            PendingActionRequest(
                actionType="purchase",
                returnPath=f"/detail/{self.listing.id}",
                anonymousSessionId=anonymous.id,
            ),
            self.db,
        )
        consumed = consume_pending_action(pending["id"], self.buyer, self.db)
        repeated = consume_pending_action(pending["id"], self.buyer, self.db)
        self.assertEqual(consumed["returnPath"], f"/detail/{self.listing.id}")
        self.assertFalse(consumed["idempotent"])
        self.assertTrue(repeated["idempotent"])
        with self.assertRaises(HTTPException) as forbidden:
            consume_pending_action(pending["id"], self.stranger, self.db)
        self.assertEqual(forbidden.exception.status_code, 403)

    def test_expired_anonymous_session_cannot_be_linked(self):
        anonymous = AnonymousSession(
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)
        )
        self.db.add(anonymous)
        self.db.commit()
        with self.assertRaises(HTTPException) as expired:
            link_anonymous_session(anonymous.id, self.buyer, self.db)
        self.assertEqual(expired.exception.status_code, 409)
        self.assertEqual(expired.exception.detail["code"], "ANONYMOUS_SESSION_EXPIRED")

    def test_anonymous_data_is_linked_only_after_explicit_consent(self):
        anonymous = AnonymousSession(consent_status="unknown")
        self.db.add(anonymous)
        self.db.commit()

        not_linked = link_anonymous_session(anonymous.id, self.buyer, self.db)
        self.assertFalse(not_linked["dataAssociated"])
        self.db.refresh(anonymous)
        self.assertIsNone(anonymous.linked_user_id)

        updated = update_anonymous_session_consent(
            anonymous.id,
            AnonymousConsentRequest(consentStatus="granted"),
            self.db,
        )
        self.assertEqual("granted", updated["consentStatus"])
        linked = link_anonymous_session(anonymous.id, self.buyer, self.db)
        self.assertTrue(linked["dataAssociated"])
        self.assertEqual(self.buyer.id, linked["linkedUserId"])

    def test_unpaid_reminder_runs_before_order_expiry(self):
        order = Order(
            buyer_id=self.buyer.id,
            seller_id=self.seller.id,
            listing_id=self.listing.id,
            status="pendingPay",
            amount=100,
            created_at=datetime.now(timezone.utc)
            - timedelta(minutes=settings.pending_pay_reminder_minutes + 1),
        )
        self.db.add(order)
        self.db.commit()
        created = _process_order_reminders(self.db)
        self.db.commit()
        reminder = (
            self.db.query(SystemNotification)
            .filter(
                SystemNotification.user_id == self.buyer.id,
                SystemNotification.notification_type == "unpaid_order_reminder",
            )
            .one()
        )
        self.assertEqual(created, 1)
        self.assertEqual(reminder.business_id, str(order.id))
        self.db.refresh(order)
        self.assertEqual(order.status, "pendingPay")

    def test_payment_deadline_reminder_is_distinct_and_idempotent(self):
        deadline_age = (
            settings.pending_pay_expire_minutes
            - settings.pending_pay_deadline_reminder_minutes_before
            + 1
        )
        order = Order(
            buyer_id=self.buyer.id,
            seller_id=self.seller.id,
            listing_id=self.listing.id,
            status="pendingPay",
            amount=100,
            created_at=datetime.now(timezone.utc) - timedelta(minutes=deadline_age),
        )
        self.db.add(order)
        self.db.commit()
        first_count = _process_order_reminders(self.db)
        second_count = _process_order_reminders(self.db)
        self.db.commit()
        deadline = (
            self.db.query(SystemNotification)
            .filter(
                SystemNotification.user_id == self.buyer.id,
                SystemNotification.notification_type == "payment_deadline_reminder",
            )
            .one()
        )
        self.assertEqual(first_count, 2)
        self.assertEqual(second_count, 0)
        self.assertEqual(deadline.business_id, str(order.id))
        self.db.refresh(order)
        self.assertEqual(order.status, "pendingPay")

    def test_scheduled_reminder_honors_transaction_reminder_setting(self):
        order = Order(
            buyer_id=self.buyer.id,
            seller_id=self.seller.id,
            listing_id=self.listing.id,
            status="pendingPay",
            amount=100,
            created_at=datetime.now(timezone.utc)
            - timedelta(minutes=settings.pending_pay_reminder_minutes + 1),
        )
        self.db.add_all(
            [order, UserSettings(user_id=self.buyer.id, remind_pay=False)]
        )
        self.db.commit()
        self.assertEqual(0, _process_order_reminders(self.db))
        self.assertEqual(0, self.db.query(SystemNotification).count())

    def test_untouched_private_offer_expires_in_scheduler(self):
        offer = PrivateOffer(
            product_id=self.listing.id,
            seller_id=self.seller.id,
            buyer_id=self.buyer.id,
            conversation_id=self.conversation.id,
            original_price=100,
            negotiated_price=90,
            currency="AUD",
            total_amount=90,
            expiration_time=datetime.now(timezone.utc) - timedelta(minutes=1),
            status="PENDING",
        )
        self.db.add(offer)
        self.db.flush()
        from app.models import Message

        message = Message(
            conversation_id=self.conversation.id,
            sender_id=self.seller.id,
            text="Private offer",
            message_type="private_offer",
            structured_payload_json=f'{{"id":"{offer.id}","status":"PENDING"}}',
        )
        self.db.add(message)
        self.db.commit()
        self.assertEqual(1, _process_expired_private_offers(self.db))
        self.db.commit()
        self.db.refresh(offer)
        self.db.refresh(message)
        self.assertEqual("EXPIRED", offer.status)
        self.assertIn('"status": "EXPIRED"', message.structured_payload_json)
        notice = (
            self.db.query(SystemNotification)
            .filter(
                SystemNotification.user_id == self.seller.id,
                SystemNotification.notification_type == "private_offer_expired",
            )
            .one()
        )
        self.assertEqual(offer.id, notice.business_id)
        self.assertEqual(0, _process_expired_private_offers(self.db))

    def test_relogin_on_same_device_rotates_refresh_without_duplicate_session(self):
        _issue_tokens(
            self.db,
            self.buyer,
            device_id="stable-device-id",
            platform="android",
            device_name="First name",
        )
        first_session = self.db.query(DeviceSession).one()
        first_refresh_id = first_session.refresh_token_id
        _issue_tokens(
            self.db,
            self.buyer,
            device_id="stable-device-id",
            platform="android",
            device_name="Updated name",
        )
        sessions = self.db.query(DeviceSession).all()
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].device_name, "Updated name")
        self.assertNotEqual(sessions[0].refresh_token_id, first_refresh_id)
        first_refresh = (
            self.db.query(RefreshToken).filter(RefreshToken.id == first_refresh_id).one()
        )
        self.assertTrue(first_refresh.revoked)

    def test_global_token_revocation_also_closes_device_sessions(self):
        _issue_tokens(
            self.db,
            self.buyer,
            device_id="revoked-device",
            platform="android",
            device_name="Buyer phone",
        )
        session = self.db.query(DeviceSession).one()
        self.assertIsNone(session.revoked_at)
        revoke_user_refresh_tokens(self.db, self.buyer.id)
        self.db.refresh(session)
        self.assertIsNotNone(session.revoked_at)
        self.assertTrue(self.db.query(RefreshToken).one().revoked)

    def test_second_successful_payment_is_refunded_instead_of_fulfilled(self):
        other_buyer = User(
            id="race-buyer",
            nickname="Race buyer",
            password_hash="test",
            heishi_id="HSRACEBUYER",
        )
        first = Order(
            buyer_id=self.buyer.id,
            seller_id=self.seller.id,
            listing_id=self.listing.id,
            status="pendingPay",
            amount=100,
            payment_status="succeeded",
            psp="stripe",
            psp_transaction_id="pi_first",
        )
        second = Order(
            buyer_id=other_buyer.id,
            seller_id=self.seller.id,
            listing_id=self.listing.id,
            status="pendingPay",
            amount=100,
            payment_status="succeeded",
            psp="stripe",
            psp_transaction_id="pi_second",
        )
        self.db.add_all((other_buyer, first, second))
        self.db.commit()
        fulfill_paid_order(self.db, first)
        with patch.object(settings, "payments_simulated", True):
            fulfill_paid_order(self.db, second)
        self.db.refresh(first)
        self.db.refresh(second)
        self.db.refresh(self.listing)
        self.assertEqual(first.status, "pendingShip")
        self.assertEqual(self.listing.status, "sold")
        self.assertEqual(second.status, "refunded")
        self.assertEqual(second.payment_status, "refunded")
        self.assertEqual(second.payout_status, "reversed")
        self.assertTrue(second.payout_paused)
        seller_ready_notifications = (
            self.db.query(SystemNotification)
            .filter(
                SystemNotification.user_id == self.seller.id,
                SystemNotification.notification_type == "buyer_payment_succeeded",
            )
            .count()
        )
        self.assertEqual(seller_ready_notifications, 1)

    def test_share_conversion_counts_paid_order_once(self):
        share = ShareRecord(
            share_token="secure-test-token",
            product_id=self.listing.id,
            sharer_user_id=self.seller.id,
        )
        order = Order(
            buyer_id=self.buyer.id,
            seller_id=self.seller.id,
            listing_id=self.listing.id,
            status="pendingShip",
            amount=100,
            payment_status="succeeded",
        )
        self.db.add_all((share, order))
        self.db.commit()
        order_event = ShareEventRequest(eventType="order", businessId=str(order.id))
        payment_event = ShareEventRequest(eventType="payment", businessId=str(order.id))
        record_share_event(share.share_token, order_event, self.buyer, self.db)
        first = record_share_event(share.share_token, payment_event, self.buyer, self.db)
        repeated = record_share_event(share.share_token, payment_event, self.buyer, self.db)
        self.db.refresh(share)
        self.assertFalse(first["idempotent"])
        self.assertTrue(repeated["idempotent"])
        self.assertEqual(share.conversion_count, 1)

    def test_guest_share_event_requires_anonymous_session(self):
        share = ShareRecord(share_token="anonymous-token", product_id=self.listing.id)
        self.db.add(share)
        self.db.commit()
        with self.assertRaises(HTTPException) as invalid:
            record_share_event(
                share.share_token,
                ShareEventRequest(eventType="open"),
                None,
                self.db,
            )
        self.assertEqual(invalid.exception.status_code, 422)

    def test_guest_share_attribution_requires_analytics_consent(self):
        share = ShareRecord(share_token="consent-share-token", product_id=self.listing.id)
        anonymous = AnonymousSession(consent_status="denied")
        self.db.add_all([share, anonymous])
        self.db.commit()

        denied = record_share_event(
            share.share_token,
            ShareEventRequest(
                eventType="open",
                anonymousSessionId=anonymous.id,
            ),
            None,
            self.db,
        )
        self.assertFalse(denied["accepted"])
        self.assertFalse(denied["recorded"])
        self.assertEqual(
            self.db.query(ShareAttributionEvent)
            .filter(ShareAttributionEvent.share_id == share.id)
            .count(),
            0,
        )

        update_anonymous_session_consent(
            anonymous.id,
            AnonymousConsentRequest(consentStatus="granted"),
            self.db,
        )
        accepted = record_share_event(
            share.share_token,
            ShareEventRequest(
                eventType="open",
                anonymousSessionId=anonymous.id,
            ),
            None,
            self.db,
        )
        self.assertTrue(accepted["accepted"])
        self.assertFalse(accepted["idempotent"])

    def test_share_registration_requires_account_created_after_share(self):
        now = datetime.now(timezone.utc)
        self.buyer.created_at = now - timedelta(days=2)
        share = ShareRecord(
            share_token="registration-attribution-token",
            product_id=self.listing.id,
            created_at=now - timedelta(days=1),
        )
        new_recipient = User(
            id="new-share-recipient",
            nickname="New Recipient",
            password_hash="test",
            heishi_id="HSNEWSHARERECIPIENT",
            created_at=now,
        )
        self.db.add_all([share, new_recipient])
        existing_session = AnonymousSession(
            consent_status="granted",
            linked_user_id=self.buyer.id,
            expires_at=now + timedelta(days=1),
        )
        new_session = AnonymousSession(
            consent_status="granted",
            linked_user_id=new_recipient.id,
            expires_at=now + timedelta(days=1),
        )
        self.db.add_all([existing_session, new_session])
        self.db.commit()

        with self.assertRaises(HTTPException) as existing_account:
            record_share_event(
                share.share_token,
                ShareEventRequest(
                    eventType="registration",
                    anonymousSessionId=existing_session.id,
                ),
                self.buyer,
                self.db,
            )
        self.assertEqual(existing_account.exception.status_code, 409)
        self.assertEqual(
            existing_account.exception.detail["code"],
            "REGISTRATION_NOT_ATTRIBUTABLE",
        )

        result = record_share_event(
            share.share_token,
            ShareEventRequest(
                eventType="registration",
                anonymousSessionId=new_session.id,
            ),
            new_recipient,
            self.db,
        )
        self.assertFalse(result["idempotent"])

    def test_share_registration_requires_causal_anonymous_session(self):
        share = ShareRecord(
            share_token="registration-session-required-token",
            product_id=self.listing.id,
            created_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
        self.db.add(share)
        self.db.commit()
        with self.assertRaises(HTTPException) as raised:
            record_share_event(
                share.share_token,
                ShareEventRequest(eventType="registration"),
                self.buyer,
                self.db,
            )
        self.assertEqual("REGISTRATION_SESSION_REQUIRED", raised.exception.detail["code"])

    def test_share_link_is_suspended_after_access_limit(self):
        share = ShareRecord(
            share_token="overused-token",
            product_id=self.listing.id,
            access_count=settings.share_max_access_count,
        )
        self.db.add(share)
        self.db.commit()
        with self.assertRaises(HTTPException) as blocked:
            resolve_share_link(share.share_token, self.db)
        self.assertEqual(blocked.exception.status_code, 429)
        self.db.refresh(share)
        self.assertEqual(share.status, "suspended")

    def test_media_rejection_hides_every_listing_using_the_asset(self):
        admin = User(
            id="admin",
            nickname="Admin",
            password_hash="test",
            heishi_id="HSADMIN",
            is_admin=True,
        )
        asset = MediaAsset(
            owner_id=self.seller.id,
            listing_id=self.listing.id,
            media_type="image",
            status="READY",
            content_type="image/jpeg",
            storage_key="seller/example.jpg",
            original_url="https://example.com/item.jpg",
            thumbnail_url="https://example.com/item-thumb.jpg",
        )
        self.db.add_all((admin, asset))
        self.db.commit()
        payload = admin_moderate_media_asset(
            asset.id,
            MediaModerationRequest(decision="reject", reason="Unsafe media"),
            admin,
            self.db,
        )
        self.db.refresh(self.listing)
        self.assertEqual(self.listing.review_status, "rejected")
        self.assertIn(self.listing.id, payload["affectedListingIds"])

    def test_server_side_duplicate_detection_does_not_need_client_checksum(self):
        content = b"same uploaded bytes"
        checksum = hashlib.sha256(content).hexdigest()
        existing = MediaAsset(
            owner_id=self.seller.id,
            media_type="image",
            status="READY",
            content_type="image/jpeg",
            file_size=len(content),
            checksum_sha256=checksum,
            storage_key="seller/existing.jpg",
            original_url="https://example.com/existing.jpg",
        )
        candidate = MediaAsset(
            owner_id=self.seller.id,
            media_type="image",
            status="UPLOADED",
            content_type="image/jpeg",
            file_size=len(content),
            storage_key="seller/candidate.jpg",
        )
        self.db.add_all((existing, candidate))
        self.db.commit()
        duplicate = _existing_duplicate_asset(self.db, candidate, content)
        self.assertEqual(duplicate.id, existing.id)
        self.assertEqual(candidate.checksum_sha256, checksum)

    def test_ready_owned_video_is_attached_to_listing_contract(self):
        image_url = "https://example.com/processed-image.jpg"
        video_url = "https://example.com/processed-video.mp4"
        image_asset = MediaAsset(
            owner_id=self.seller.id,
            media_type="image",
            status="READY",
            moderation_status="approved",
            content_type="image/jpeg",
            storage_key="seller/image.jpg",
            original_url=image_url,
        )
        video_asset = MediaAsset(
            owner_id=self.seller.id,
            media_type="video",
            status="READY",
            moderation_status="approved",
            content_type="video/mp4",
            storage_key="seller/video.mp4",
            original_url=video_url,
        )
        self.db.add_all((image_asset, video_asset))
        self.db.commit()
        created = create_listing(
            CreateListingRequest(
                type="product",
                title="Listing with video",
                description="Video contract test",
                price=20,
                categoryKey="home",
                locationLabel="Melbourne",
                imageUrls=[image_url],
                videoUrls=[video_url],
            ),
            self.seller,
            self.db,
        )
        stored = self.db.query(Listing).filter(Listing.id == created.id).one()
        self.assertEqual(created.videos, [video_url])
        self.assertEqual(stored.videos, [video_url])
        self.assertEqual(image_asset.listing_id, stored.id)
        self.assertEqual(video_asset.listing_id, stored.id)


if __name__ == "__main__":
    unittest.main()
