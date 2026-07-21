import asyncio
import subprocess
import tempfile
import unittest
from io import BytesIO
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from starlette.requests import Request
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from PIL import Image

from app.auth import (
    _user_from_legacy_token,
    create_access_token,
    hash_password,
    normalize_phone,
)
from app.account_merge import account_has_cross_domain_state
from app.background_jobs import acquire_scheduler_lease, run_periodic_cycle
from app.config import settings
from app.database import Base
from app.media_security import MediaSecurityError, scan_media_for_threats
from app.migrations import _backfill_expanded_deduplication_keys, run_migrations
from app.main import public_share_landing
from app.models import (
    AdminConversation,
    AnonymousSession,
    AuthIdentity,
    DeviceSession,
    DevicePushToken,
    ExposureRule,
    Favorite,
    Follow,
    FollowedCategory,
    Listing,
    LoginAuditLog,
    MediaAsset,
    NotificationPreference,
    Order,
    PhoneOtp,
    SearchLog,
    SystemNotification,
    ShareRecord,
    ShareAttributionEvent,
    User,
    UserSettings,
    UploadSession,
)
from app.catalog_helpers import (
    apply_feed_listing_status_filter,
    apply_feed_sort,
    apply_public_listing_visibility_filter,
)
from app.routers.catalog import _personalized_viewer_id
from app.routers.users import get_public_listings, get_public_profile
from app.notification_jobs import _process_interest_notifications
from app.payments.refunds import apply_paypal_refund_update
from app.payout_release import release_payout_for_order
from app.routers.auth import (
    alipay_login,
    bind_alipay_identity,
    login,
    login_verify,
    merge_phone_account,
    merge_third_party_account,
    wechat_login,
)
from app.routers.admin_routes import AdminMergeAccountsRequest, merge_user_accounts
from app.routers.platform_features import (
    AdminBroadcastRequest,
    AdminOpenConversationRequest,
    CreateUploadSessionRequest,
    ShareEventRequest,
    _exposure_payload,
    _scan_media_signature,
    admin_open_support_conversation,
    create_role_announcement,
    create_upload_session,
    finalize_resumable_upload,
    process_queued_media,
    record_share_event,
    retry_media_processing,
    upload_media_chunk,
)
from app.routers.listings import _validate_owned_ready_media
from app.schemas import (
    AlipayAuthRequest,
    LoginRequest,
    LoginOtpRequest,
    MergePhoneAccountRequest,
    MergeThirdPartyAccountRequest,
    WeChatAuthRequest,
)
from app.serializers import listing_to_summary
from app.storage import upload_bytes_at_key
from app.video_processing import process_video_variants, video_processor_available


class ExpandedRequirementCompletionTests(unittest.TestCase):
    @staticmethod
    def _request() -> Request:
        return Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/",
                "headers": [(b"user-agent", b"audit-test")],
                "client": ("127.0.0.1", 12345),
            }
        )

    def test_token_issuance_rejects_every_restricted_account_status(self):
        from app.routers.auth import _issue_tokens

        for index, status in enumerate(("suspended", "banned", "merged"), start=1):
            user = User(
                id=f"restricted-token-user-{index}",
                nickname=f"Restricted {index}",
                phone=f"+1415555267{index}",
                password_hash=hash_password("Password123!"),
                heishi_id=f"HSRESTRICTED{index}",
                account_status=status,
            )
            self.db.add(user)
            self.db.commit()
            with self.assertRaises(HTTPException) as raised:
                _issue_tokens(self.db, user)
            self.assertEqual(403, raised.exception.status_code)
            self.assertEqual(
                "ACCOUNT_SUSPENDED",
                raised.exception.detail["code"],
            )
            self.assertEqual(
                status,
                raised.exception.detail["details"]["accountStatus"],
            )

    def test_merge_guard_detects_user_library_state(self):
        listing = Listing(
            title="Merge guard listing",
            description="Regression fixture",
            price=10,
            category_key="other",
            condition_key="good",
            location_label="Melbourne",
            image_url="https://example.test/listing.jpg",
            seller_id=self.seller.id,
            status="active",
            review_status="approved",
        )
        self.db.add(listing)
        self.db.flush()
        self.db.add(Favorite(user_id=self.buyer.id, listing_id=listing.id))
        self.db.commit()
        self.assertTrue(account_has_cross_domain_state(self.db, self.buyer.id))

    def test_restricted_provider_login_is_audited_with_accurate_provider(self):
        from app.routers.auth import _issue_tokens

        self.seller.account_status = "suspended"
        self.db.commit()
        with self.assertRaises(HTTPException):
            _issue_tokens(
                self.db,
                self.seller,
                request=self._request(),
                device_id="restricted-device",
                auth_provider="wechat",
            )
        audit = self.db.query(LoginAuditLog).one()
        self.assertEqual("wechat", audit.provider)
        self.assertEqual("ACCOUNT_SUSPENDED", audit.failure_code)
        self.assertEqual(self.seller.id, audit.user_id)

    def test_restricted_password_login_is_audited(self):
        self.seller.phone = "+61400000009"
        self.seller.password_hash = hash_password("Password123!")
        self.seller.account_status = "suspended"
        self.db.commit()
        with self.assertRaises(HTTPException):
            login(
                LoginRequest(
                    phone=self.seller.phone,
                    password="Password123!",
                    deviceId="password-device",
                ),
                request=self._request(),
                db=self.db,
            )
        audit = self.db.query(LoginAuditLog).one()
        self.assertEqual("phone_password", audit.provider)
        self.assertEqual("ACCOUNT_SUSPENDED", audit.failure_code)

    def test_success_audit_records_actual_authentication_provider(self):
        from app.routers.auth import _issue_tokens

        _issue_tokens(
            self.db,
            self.seller,
            request=self._request(),
            device_id="provider-device",
            auth_provider="alipay",
        )
        audit = self.db.query(LoginAuditLog).one()
        self.assertEqual("alipay", audit.provider)
        self.assertTrue(audit.success)

    @patch("builtins.print")
    def test_otp_is_not_printed_when_dev_exposure_is_disabled(self, mocked_print):
        from app.routers.auth import _send_login_code

        self.seller.phone = "+61412345678"
        self.db.commit()
        with patch.object(settings, "expose_dev_otp", False):
            _send_login_code(self.db, self.seller.phone)
        mocked_print.assert_not_called()

    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.seller = User(
            id="seller",
            heishi_id="HM-SELLER",
            nickname="Seller",
            password_hash="x",
            account_status="normal",
        )
        self.buyer = User(
            id="buyer",
            heishi_id="HM-BUYER",
            nickname="Buyer",
            password_hash="x",
            account_status="normal",
        )
        self.db.add_all([self.seller, self.buyer])
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_access_token_is_rejected_immediately_after_device_revocation(self):
        session = DeviceSession(
            user_id=self.seller.id,
            device_id="device-under-test",
            platform="android",
        )
        self.db.add(session)
        self.db.commit()
        token = create_access_token(self.seller.id, session.id)
        self.assertEqual(
            self.seller.id,
            _user_from_legacy_token(token, self.db).id,
        )
        session.revoked_at = datetime.now(timezone.utc)
        self.db.commit()
        self.assertIsNone(_user_from_legacy_token(token, self.db))

    def test_periodic_scheduler_lease_is_exclusive_and_recovers_after_crash_timeout(self):
        now = datetime.now(timezone.utc)
        self.assertTrue(
            acquire_scheduler_lease(
                self.db,
                owner_id="worker-one",
                now=now,
                interval_seconds=10,
                crash_lease_seconds=30,
            )
        )
        competing = Session(self.engine)
        try:
            self.assertFalse(
                acquire_scheduler_lease(
                    competing,
                    owner_id="worker-two",
                    now=now + timedelta(seconds=11),
                    interval_seconds=10,
                    crash_lease_seconds=30,
                )
            )
            self.assertTrue(
                acquire_scheduler_lease(
                    competing,
                    owner_id="worker-two",
                    now=now + timedelta(seconds=31),
                    interval_seconds=10,
                    crash_lease_seconds=30,
                )
            )
        finally:
            competing.close()

    def test_periodic_scheduler_isolates_failure_and_runs_future_cycles(self):
        calls: list[str] = []

        def broken(db):
            calls.append("broken")
            db.add(SearchLog(term="must-rollback", user_id=self.buyer.id))
            raise RuntimeError("simulated job failure")

        def healthy(db):
            calls.append("healthy")
            db.add(SearchLog(term="healthy", user_id=self.buyer.id))
            db.commit()

        now = datetime.now(timezone.utc)
        first = run_periodic_cycle(
            self.db,
            owner_id="resilient-worker",
            jobs=(("broken", broken), ("healthy", healthy)),
            now=now,
            interval_seconds=10,
        )
        self.assertEqual(["broken"], first["failed"])
        self.assertEqual(["healthy"], first["completed"])
        self.assertEqual(
            ["healthy"],
            [row.term for row in self.db.query(SearchLog).order_by(SearchLog.id).all()],
        )

        second = run_periodic_cycle(
            self.db,
            owner_id="resilient-worker",
            jobs=(("healthy", healthy),),
            now=now + timedelta(seconds=11),
            interval_seconds=10,
        )
        self.assertTrue(second["acquired"])
        self.assertEqual(2, calls.count("healthy"))

    def test_admin_can_merge_empty_duplicate_into_historical_account(self):
        admin = User(id="admin-merge", heishi_id="HM-ADMIN-MERGE", nickname="Admin", password_hash="x", is_admin=True)
        duplicate = User(
            id="duplicate",
            heishi_id="HM-DUPLICATE",
            nickname="Duplicate",
            password_hash="x",
            phone="+61400000001",
            phone_verified=True,
            account_status="normal",
        )
        self.db.add_all([admin, duplicate])
        self.db.add(Listing(seller_id=self.seller.id, title="Historical listing", price=10, category_key="misc", location_label="Melbourne", image_url="", status="active"))
        session = DeviceSession(user_id=duplicate.id, device_id="duplicate-device", platform="android")
        self.db.add(session)
        self.db.commit()

        result = merge_user_accounts(
            AdminMergeAccountsRequest(sourceUserId=duplicate.id, destinationUserId=self.seller.id),
            db=self.db,
            admin=admin,
        )

        self.assertTrue(result["ok"])
        self.db.refresh(duplicate)
        self.db.refresh(self.seller)
        self.db.refresh(session)
        self.assertEqual("merged", duplicate.account_status)
        self.assertEqual("+61400000001", self.seller.phone)
        self.assertIsNotNone(session.revoked_at)
        identity = self.db.query(AuthIdentity).filter(AuthIdentity.provider == "phone").one()
        self.assertEqual(self.seller.id, identity.user_id)

    def test_admin_merge_migrates_source_marketplace_history(self):
        admin = User(id="admin-merge-2", heishi_id="HM-ADMIN-MERGE2", nickname="Admin", password_hash="x", is_admin=True)
        duplicate = User(id="duplicate-history", heishi_id="HM-DUP-HISTORY", nickname="Duplicate", password_hash="x")
        self.db.add_all([admin, duplicate])
        listing = Listing(seller_id=duplicate.id, title="Owned listing", price=10, category_key="misc", location_label="Melbourne", image_url="", status="active")
        self.db.add(listing)
        self.db.commit()
        result = merge_user_accounts(
            AdminMergeAccountsRequest(sourceUserId=duplicate.id, destinationUserId=self.seller.id),
            db=self.db,
            admin=admin,
        )
        self.db.refresh(listing)
        self.db.refresh(duplicate)
        self.assertTrue(result["ok"])
        self.assertEqual(self.seller.id, listing.seller_id)
        self.assertEqual("merged", duplicate.account_status)

    def test_admin_merge_migrates_notification_state(self):
        admin = User(id="admin-merge-state", heishi_id="HM-ADMIN-STATE", nickname="Admin", password_hash="x", is_admin=True)
        duplicate = User(id="duplicate-state", heishi_id="HM-DUP-STATE", nickname="Duplicate", password_hash="x")
        self.db.add_all([admin, duplicate])
        notification = SystemNotification(
                user_id=duplicate.id,
                title="Existing notice",
                body="This must not be stranded.",
            )
        self.db.add(notification)
        self.db.commit()
        merge_user_accounts(
            AdminMergeAccountsRequest(sourceUserId=duplicate.id, destinationUserId=self.seller.id),
            db=self.db,
            admin=admin,
        )
        self.db.refresh(notification)
        self.assertEqual(self.seller.id, notification.user_id)

    def test_admin_merge_rejects_restricted_source_or_destination(self):
        admin = User(id="admin-merge-3", heishi_id="HM-ADMIN-MERGE3", nickname="Admin", password_hash="x", is_admin=True)
        duplicate = User(
            id="duplicate-restricted",
            heishi_id="HM-DUP-RESTRICTED",
            nickname="Duplicate",
            password_hash="x",
            account_status="suspended",
        )
        self.db.add_all([admin, duplicate])
        self.db.commit()
        with self.assertRaises(HTTPException) as source_rejected:
            merge_user_accounts(
                AdminMergeAccountsRequest(
                    sourceUserId=duplicate.id,
                    destinationUserId=self.seller.id,
                ),
                db=self.db,
                admin=admin,
            )
        self.assertEqual(
            "ACCOUNT_NOT_MERGEABLE",
            source_rejected.exception.detail["code"],
        )

        duplicate.account_status = "normal"
        self.seller.account_status = "banned"
        self.db.commit()
        with self.assertRaises(HTTPException) as destination_rejected:
            merge_user_accounts(
                AdminMergeAccountsRequest(
                    sourceUserId=duplicate.id,
                    destinationUserId=self.seller.id,
                ),
                db=self.db,
                admin=admin,
            )
        self.assertEqual(
            "ACCOUNT_NOT_MERGEABLE",
            destination_rejected.exception.detail["code"],
        )

    def test_admin_merge_rejects_accounts_that_are_transaction_counterparties(self):
        admin = User(id="admin-counterpart", heishi_id="HM-ADMIN-COUNTERPART", nickname="Admin", password_hash="x", is_admin=True)
        duplicate = User(id="counterpart-duplicate", heishi_id="HM-COUNTERPART-DUP", nickname="Duplicate", password_hash="x")
        listing = Listing(seller_id=self.seller.id, title="Counterpart listing", price=10, category_key="misc", location_label="Melbourne", image_url="", status="active")
        self.db.add_all([admin, duplicate, listing])
        self.db.flush()
        self.db.add(Order(buyer_id=duplicate.id, seller_id=self.seller.id, listing_id=listing.id, amount=10))
        self.db.commit()
        with self.assertRaises(HTTPException) as raised:
            merge_user_accounts(
                AdminMergeAccountsRequest(sourceUserId=duplicate.id, destinationUserId=self.seller.id),
                db=self.db,
                admin=admin,
            )
        self.assertEqual("MERGE_COUNTERPART_CONFLICT", raised.exception.detail["code"])
        self.db.refresh(duplicate)
        self.assertEqual("normal", duplicate.account_status)

    def test_user_authorized_phone_merge_cannot_bypass_account_suspension(self):
        restricted = User(
            id="restricted-phone-merge",
            heishi_id="HM-RESTRICTED-PHONE",
            nickname="Restricted",
            password_hash=hash_password("correct-password"),
            phone="+61411111111",
            phone_verified=True,
            account_status="suspended",
        )
        self.db.add(restricted)
        self.db.commit()
        request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
        with self.assertRaises(HTTPException) as rejected:
            merge_phone_account(
                MergePhoneAccountRequest(
                    phone="+61411111111",
                    password="correct-password",
                ),
                request=request,
                user=self.seller,
                db=self.db,
            )
        self.assertEqual("MERGE_ACCOUNT_RESTRICTED", rejected.exception.detail["code"])

    def test_user_authorized_phone_merge_retires_duplicate_as_merged(self):
        duplicate = User(
            id="normal-phone-merge",
            heishi_id="HM-NORMAL-PHONE",
            nickname="Duplicate",
            password_hash=hash_password("correct-password"),
            phone="+61422222222",
            phone_verified=True,
            account_status="normal",
        )
        self.db.add(duplicate)
        self.db.commit()
        request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
        merge_phone_account(
            MergePhoneAccountRequest(
                phone="+61422222222",
                password="correct-password",
            ),
            request=request,
            user=self.seller,
            db=self.db,
        )
        self.db.refresh(duplicate)
        self.db.refresh(self.seller)
        self.assertEqual("merged", duplicate.account_status)
        self.assertEqual("+61422222222", self.seller.phone)

    def test_user_authorized_phone_merge_rejects_notification_preferences(self):
        duplicate = User(
            id="phone-merge-with-preferences",
            heishi_id="HM-PHONE-PREFERENCES",
            nickname="Duplicate",
            password_hash=hash_password("correct-password"),
            phone="+61433333333",
            phone_verified=True,
            account_status="normal",
        )
        self.db.add(duplicate)
        self.db.flush()
        self.db.add(
            NotificationPreference(
                user_id=duplicate.id,
                user_role_context="buyer",
                category="marketing",
                in_app_enabled=False,
                push_enabled=False,
            )
        )
        self.db.commit()
        request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
        with self.assertRaises(HTTPException) as rejected:
            merge_phone_account(
                MergePhoneAccountRequest(phone="+61433333333", password="correct-password"),
                request=request,
                user=self.seller,
                db=self.db,
            )
        self.assertEqual("MERGE_REQUIRES_SUPPORT", rejected.exception.detail["code"])

    @patch(
        "app.routers.auth.exchange_authorization_code",
        return_value={"user_id": "restricted-alipay-subject", "access_token": "token"},
    )
    def test_user_authorized_provider_merge_cannot_bypass_account_suspension(self, _exchange):
        restricted = User(
            id="restricted-provider-merge",
            heishi_id="HM-RESTRICTED-PROVIDER",
            nickname="Restricted",
            password_hash="x",
            account_status="banned",
        )
        self.db.add(restricted)
        self.db.add(
            AuthIdentity(
                user_id=restricted.id,
                provider="alipay",
                provider_subject="restricted-alipay-subject",
                verified=True,
            )
        )
        self.db.commit()
        with self.assertRaises(HTTPException) as rejected:
            merge_third_party_account(
                MergeThirdPartyAccountRequest(
                    provider="alipay",
                    authorizationCode="authorized-code",
                ),
                user=self.seller,
                db=self.db,
            )
        self.assertEqual("MERGE_ACCOUNT_RESTRICTED", rejected.exception.detail["code"])

    @patch(
        "app.routers.auth.exchange_authorization_code",
        return_value={"user_id": "provider-with-share-state", "access_token": "token"},
    )
    def test_user_authorized_provider_merge_rejects_share_state(self, _exchange):
        duplicate = User(
            id="provider-merge-with-share",
            heishi_id="HM-PROVIDER-SHARE",
            nickname="Duplicate",
            password_hash="x",
            account_status="normal",
        )
        listing = Listing(
            seller_id=self.seller.id,
            title="Shared listing",
            price=10,
            category_key="misc",
            location_label="Melbourne",
            image_url="",
            status="active",
        )
        self.db.add_all([duplicate, listing])
        self.db.flush()
        self.db.add_all(
            [
                AuthIdentity(
                    user_id=duplicate.id,
                    provider="alipay",
                    provider_subject="provider-with-share-state",
                    verified=True,
                ),
                ShareRecord(
                    share_token="provider-share-state-token",
                    product_id=listing.id,
                    sharer_user_id=duplicate.id,
                ),
            ]
        )
        self.db.commit()
        with self.assertRaises(HTTPException) as rejected:
            merge_third_party_account(
                MergeThirdPartyAccountRequest(provider="alipay", authorizationCode="authorized-code"),
                user=self.seller,
                db=self.db,
            )
        self.assertEqual("MERGE_REQUIRES_SUPPORT", rejected.exception.detail["code"])

    @patch(
        "app.routers.auth.exchange_authorization_code",
        return_value={"user_id": "2088123412341234", "access_token": "token"},
    )
    def test_alipay_binding_requires_verified_provider_identity(self, _exchange):
        result = bind_alipay_identity(
            AlipayAuthRequest(authCode="authorized-code"),
            user=self.seller,
            db=self.db,
        )
        self.assertEqual({"bound": True, "provider": "alipay"}, result)
        identity = (
            self.db.query(AuthIdentity)
            .filter(AuthIdentity.user_id == self.seller.id, AuthIdentity.provider == "alipay")
            .one()
        )
        self.assertTrue(identity.verified)
        self.assertEqual("2088123412341234", identity.provider_subject)
        self.assertTrue(self.seller.alipay_bound)

        with self.assertRaises(HTTPException) as raised:
            bind_alipay_identity(
                AlipayAuthRequest(authCode="same-provider-identity"),
                user=self.buyer,
                db=self.db,
            )
        self.assertEqual(409, raised.exception.status_code)

    @patch(
        "app.routers.auth.exchange_authorization_code",
        return_value={"user_id": "2088999911112222", "nick_name": "Alipay Buyer"},
    )
    def test_alipay_authorization_can_register_and_sign_in(self, _exchange):
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/v1/auth/alipay",
                "headers": [],
                "client": ("127.0.0.1", 12345),
            }
        )
        body = AlipayAuthRequest(
            authCode="verified-alipay-code",
            city="Melbourne",
            deviceId="alipay-device",
            platform="android",
        )
        first = alipay_login(body, request=request, db=self.db)
        second = alipay_login(body, request=request, db=self.db)
        self.assertTrue(first.accessToken)
        self.assertTrue(second.accessToken)
        identity = (
            self.db.query(AuthIdentity)
            .filter(
                AuthIdentity.provider == "alipay",
                AuthIdentity.provider_subject == "2088999911112222",
            )
            .one()
        )
        registered = self.db.query(User).filter(User.id == identity.user_id).one()
        self.assertEqual("Melbourne", registered.city)
        self.assertTrue(registered.alipay_bound)

    def test_media_security_scan_rejects_executable_and_type_mismatch(self):
        _scan_media_signature("image", "image/jpeg", b"\xff\xd8\xff\xe0valid")
        with self.assertRaisesRegex(ValueError, "Unsafe"):
            _scan_media_signature("image", "image/jpeg", b"MZ\x90\x00executable")
        with self.assertRaisesRegex(ValueError, "do not match"):
            _scan_media_signature("image", "image/png", b"\xff\xd8\xff\xe0jpeg")

    def test_owner_cannot_retry_admin_rejected_media(self):
        asset = MediaAsset(
            owner_id=self.seller.id,
            media_type="image",
            status="REJECTED",
            content_type="image/jpeg",
            storage_key="rejected/original.jpg",
            original_url="https://cdn.example/rejected.jpg",
            moderation_status="rejected",
        )
        self.db.add(asset)
        self.db.commit()
        with self.assertRaises(HTTPException) as raised:
            retry_media_processing(asset.id, user=self.seller, db=self.db)
        self.assertEqual(409, raised.exception.status_code)
        self.db.refresh(asset)
        self.assertEqual("REJECTED", asset.status)
        self.assertEqual("rejected", asset.moderation_status)

    def test_failed_otp_login_is_audited(self):
        phone = "+61412345678"
        self.seller.phone = phone
        self.seller.phone_verified = True
        self.db.add(
            PhoneOtp(
                phone=phone,
                purpose="login",
                code_hash="not-the-entered-code",
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            )
        )
        self.db.commit()
        with self.assertRaises(HTTPException):
            login_verify(
                LoginOtpRequest(
                    phone=phone,
                    verificationCode="123456",
                    deviceId="otp-device",
                ),
                request=self._request(),
                db=self.db,
            )
        audit = self.db.query(LoginAuditLog).one()
        self.assertEqual("phone", audit.provider)
        self.assertEqual("OTP_INVALID", audit.failure_code)
        self.assertEqual("otp-device", audit.device_id)

    @patch("app.routers.auth._wechat_exchange_code")
    def test_failed_wechat_login_is_audited(self, exchange):
        exchange.side_effect = HTTPException(
            status_code=401,
            detail={"code": "WECHAT_AUTH_FAILED", "message": "invalid"},
        )
        with self.assertRaises(HTTPException):
            wechat_login(
                WeChatAuthRequest(code="bad-code", deviceId="wechat-device"),
                request=self._request(),
                db=self.db,
            )
        audit = self.db.query(LoginAuditLog).one()
        self.assertEqual("wechat", audit.provider)
        self.assertEqual("WECHAT_AUTH_FAILED", audit.failure_code)
        self.assertEqual("wechat-device", audit.device_id)

    @patch("app.routers.auth.exchange_authorization_code")
    def test_failed_alipay_login_is_audited(self, exchange):
        from app.alipay_payout_service import AlipayPayoutError

        exchange.side_effect = AlipayPayoutError("invalid authorization")
        with self.assertRaises(HTTPException):
            alipay_login(
                AlipayAuthRequest(authCode="bad-code", deviceId="alipay-device"),
                request=self._request(),
                db=self.db,
            )
        audit = self.db.query(LoginAuditLog).one()
        self.assertEqual("alipay", audit.provider)
        self.assertEqual("ALIPAY_AUTH_FAILED", audit.failure_code)
        self.assertEqual("alipay-device", audit.device_id)

    @patch("app.routers.auth.exchange_authorization_code", return_value={})
    def test_alipay_missing_identity_failure_is_audited(self, _exchange):
        with self.assertRaises(HTTPException) as raised:
            alipay_login(
                AlipayAuthRequest(authCode="missing-identity", deviceId="alipay-missing"),
                request=self._request(),
                db=self.db,
            )
        self.assertEqual("ALIPAY_IDENTITY_MISSING", raised.exception.detail["code"])
        audit = self.db.query(LoginAuditLog).one()
        self.assertEqual("alipay", audit.provider)
        self.assertEqual("ALIPAY_IDENTITY_MISSING", audit.failure_code)
        self.assertEqual("alipay-missing", audit.device_id)

    @patch(
        "app.routers.auth.exchange_authorization_code",
        return_value={"user_id": "orphaned-alipay-subject"},
    )
    def test_alipay_orphaned_identity_failure_is_audited(self, _exchange):
        self.db.add(
            AuthIdentity(
                user_id="missing-user",
                provider="alipay",
                provider_subject="orphaned-alipay-subject",
                verified=True,
            )
        )
        self.db.commit()
        with self.assertRaises(HTTPException) as raised:
            alipay_login(
                AlipayAuthRequest(authCode="orphaned-identity", deviceId="alipay-orphan"),
                request=self._request(),
                db=self.db,
            )
        self.assertEqual("AUTH_IDENTITY_ORPHANED", raised.exception.detail["code"])
        audit = self.db.query(LoginAuditLog).one()
        self.assertEqual("alipay", audit.provider)
        self.assertEqual("AUTH_IDENTITY_ORPHANED", audit.failure_code)
        self.assertEqual("missing-user", audit.user_id)
        self.assertEqual("alipay-orphan", audit.device_id)

    def test_production_media_scanner_fails_closed_when_unavailable(self):
        old_mode = settings.media_security_scan_mode
        try:
            settings.media_security_scan_mode = "clamav"
            with patch("app.media_security.socket.create_connection", side_effect=OSError):
                with self.assertRaisesRegex(MediaSecurityError, "unavailable"):
                    scan_media_for_threats(b"\xff\xd8\xff\xe0image")
        finally:
            settings.media_security_scan_mode = old_mode

    def test_paypal_refund_is_not_completed_until_provider_completion(self):
        order = Order(
            buyer_id=self.buyer.id,
            seller_id=self.seller.id,
            listing_id=1,
            amount=12,
            escrow_fee=0,
            status="refundInProgress",
            payment_status="succeeded",
            psp="paypal",
            payout_status="pending",
        )
        pending = apply_paypal_refund_update(
            order,
            {"id": "R-1", "status": "PENDING"},
        )
        self.assertEqual("pending", pending.status)
        self.assertEqual("succeeded", order.payment_status)
        self.assertIsNone(order.refunded_at)

        completed = apply_paypal_refund_update(
            order,
            {"id": "R-1", "status": "COMPLETED"},
        )
        self.assertEqual("refunded", completed.status)
        self.assertEqual("refunded", order.payment_status)
        self.assertIsNotNone(order.refunded_at)

    def test_blocked_settlement_creates_mandatory_seller_notification(self):
        order = Order(
            id=42,
            buyer_id=self.buyer.id,
            seller_id=self.seller.id,
            listing_id=1,
            amount=25,
            escrow_fee=0,
            status="pendingReview",
            payment_status="succeeded",
            psp="stripe",
            payout_status="pending",
        )
        self.db.add(order)
        self.db.flush()
        transition = release_payout_for_order(self.db, order)
        self.assertEqual("blocked", transition.status)
        notice = (
            self.db.query(SystemNotification)
            .filter(
                SystemNotification.user_id == self.seller.id,
                SystemNotification.notification_type == "seller_payout_blocked",
            )
            .one()
        )
        self.assertEqual("order", notice.business_type)

    def test_adaptive_storage_preserves_relative_object_layout(self):
        with tempfile.TemporaryDirectory() as temp:
            old_backend = settings.storage_backend
            old_dir = settings.upload_dir
            old_base = settings.base_url
            try:
                settings.storage_backend = "local"
                settings.upload_dir = temp
                settings.base_url = "http://localhost:8001"
                url, key = upload_bytes_at_key(
                    b"#EXTM3U\n",
                    "application/vnd.apple.mpegurl",
                    "users/seller/media/asset/hls/master.m3u8",
                )
                self.assertEqual(
                    "users/seller/media/asset/hls/master.m3u8",
                    key,
                )
                self.assertTrue((Path(temp) / key).is_file())
                self.assertTrue(url.endswith(key))
                with self.assertRaises(HTTPException):
                    upload_bytes_at_key(b"x", "text/plain", "../escape.txt")
            finally:
                settings.storage_backend = old_backend
                settings.upload_dir = old_dir
                settings.base_url = old_base

    def test_share_landing_preserves_android_install_attribution(self):
        token = "share_token_abcdefghijklmnopqrstuvwxyz"
        response = public_share_landing(token)
        html = response.body.decode("utf-8")
        self.assertIn(f"【HeyMarket】{token}", html)
        self.assertIn(
            f"id={settings.android_app_package}&amp;referrer=share_token%3D{token}",
            html.replace("&", "&amp;"),
        )
        self.assertIn(f"heymarket://shares/{token}", html)

    def test_share_landing_preserves_ios_install_attribution_via_clipboard(self):
        token = "ios_share_token_abcdefghijklmnopqrstuvwxyz"
        with patch.object(
            settings,
            "ios_app_store_url",
            "https://apps.apple.com/app/heymarket/id123456789",
        ):
            response = public_share_landing(token)
        html = response.body.decode("utf-8")
        self.assertIn("Install HeyMarket for iPhone", html)
        self.assertIn("copyAndInstall", html)
        self.assertIn(f"【HeyMarket】{token}", html)
        self.assertIn(
            "https://apps.apple.com/app/heymarket/id123456789",
            html,
        )

    def test_listing_cards_use_thumbnail_but_keep_detail_media(self):
        listing = Listing(
            id=99,
            seller_id=self.seller.id,
            type="product",
            title="High resolution item",
            description="",
            price=20,
            category_key="digital",
            tag_key="",
            location_label="Melbourne",
            image_url="https://cdn.example/original.jpg",
            thumbnail_url="https://cdn.example/thumbnail.webp",
            status="active",
            review_status="approved",
        )
        listing.images = [
            "https://cdn.example/preview.webp",
            "https://cdn.example/second-preview.webp",
        ]
        listing.seller = self.seller
        listing.created_at = self.seller.created_at
        payload = listing_to_summary(listing)
        self.assertEqual("https://cdn.example/thumbnail.webp", payload.imageUrl)
        self.assertEqual(
            [
                "https://cdn.example/preview.webp",
                "https://cdn.example/second-preview.webp",
            ],
            payload.images,
        )

    @patch("app.routers.platform_features.create_signed_upload")
    def test_video_can_force_resumable_upload_in_object_storage(self, signed_upload):
        create_upload_session(
            CreateUploadSessionRequest(
                mediaType="video",
                contentType="video/mp4",
                filename="clip.mp4",
                fileSize=1024,
                resumablePreferred=True,
            ),
            user=self.seller,
            db=self.db,
        )
        signed_upload.assert_not_called()

    def test_pending_moderation_media_cannot_be_attached_to_listing(self):
        asset = MediaAsset(
            owner_id=self.seller.id,
            media_type="image",
            status="READY",
            moderation_status="pending",
            content_type="image/jpeg",
            original_url="https://cdn.example/pending.jpg",
            storage_key="pending/original.jpg",
        )
        self.db.add(asset)
        self.db.commit()
        with self.assertRaises(HTTPException) as raised:
            _validate_owned_ready_media(
                self.db,
                owner_id=self.seller.id,
                image_urls=[asset.original_url],
                expected_media_type="image",
            )
        self.assertEqual(422, raised.exception.status_code)
        self.assertEqual("MEDIA_ASSET_NOT_READY", raised.exception.detail["code"])

        asset.moderation_status = "approved"
        self.db.commit()
        accepted = _validate_owned_ready_media(
            self.db,
            owner_id=self.seller.id,
            image_urls=[asset.original_url],
            expected_media_type="image",
        )
        self.assertEqual([asset.id], [row.id for row in accepted])

    def test_resumable_image_upload_reports_progress_and_finalizes(self):
        image = Image.new("RGB", (32, 24), color=(25, 75, 125))
        payload = BytesIO()
        image.save(payload, format="JPEG", quality=90)
        content = payload.getvalue()

        def request_with_body(body: bytes) -> Request:
            delivered = False

            async def receive():
                nonlocal delivered
                if delivered:
                    return {"type": "http.request", "body": b"", "more_body": False}
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}

            return Request(
                {
                    "type": "http",
                    "method": "PUT",
                    "path": "/v1/media/upload-sessions/test/chunk",
                    "headers": [],
                    "client": ("127.0.0.1", 12345),
                },
                receive,
            )

        with tempfile.TemporaryDirectory() as temp, (
            patch.object(settings, "storage_backend", "local")
        ), patch.object(settings, "upload_dir", temp), patch.object(
            settings, "base_url", "http://testserver"
        ):
            created = create_upload_session(
                CreateUploadSessionRequest(
                    mediaType="image",
                    contentType="image/jpeg",
                    filename="resumable.jpg",
                    fileSize=len(content),
                    resumablePreferred=True,
                ),
                user=self.seller,
                db=self.db,
            )
            session_id = created["uploadSession"]["id"]
            split = len(content) // 2
            first = asyncio.run(
                upload_media_chunk(
                    session_id,
                    request_with_body(content[:split]),
                    offset=0,
                    user=self.seller,
                    db=self.db,
                )
            )
            self.assertEqual(split, first["uploadSession"]["bytesUploaded"])
            self.assertEqual("UPLOADING", first["uploadSession"]["status"])
            second = asyncio.run(
                upload_media_chunk(
                    session_id,
                    request_with_body(content[split:]),
                    offset=split,
                    user=self.seller,
                    db=self.db,
                )
            )
            self.assertEqual(len(content), second["uploadSession"]["bytesUploaded"])
            self.assertEqual("UPLOADED", second["uploadSession"]["status"])
            finalized = finalize_resumable_upload(
                session_id,
                user=self.seller,
                db=self.db,
            )
            self.assertEqual("PROCESSING", finalized["status"])
            self.assertEqual("PROCESSING", finalized["uploadSession"]["status"])

    def test_media_processing_is_durable_and_runs_outside_upload_request(self):
        with tempfile.TemporaryDirectory() as temp:
            image = Image.new("RGB", (640, 480), color=(20, 120, 220))
            payload = BytesIO()
            image.save(payload, format="JPEG", quality=95)
            content = payload.getvalue()
            with (
                patch.object(settings, "storage_backend", "local"),
                patch.object(settings, "upload_dir", temp),
                patch.object(settings, "base_url", "http://testserver"),
            ):
                original_url, storage_key = upload_bytes_at_key(
                    content,
                    "image/jpeg",
                    "staging/durable.jpg",
                )
                asset = MediaAsset(
                    owner_id=self.seller.id,
                    media_type="image",
                    status="PROCESSING",
                    content_type="image/jpeg",
                    file_size=len(content),
                    storage_key=storage_key,
                    source_storage_key=storage_key,
                    source_url=original_url,
                    original_url=original_url,
                )
                self.db.add(asset)
                self.db.flush()
                upload = UploadSession(
                    media_asset_id=asset.id,
                    owner_id=self.seller.id,
                    status="PROCESSING",
                    bytes_uploaded=len(content),
                    total_bytes=len(content),
                    expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                )
                self.db.add(upload)
                self.db.commit()

                processed = process_queued_media(self.db)
                self.db.refresh(asset)
                self.db.refresh(upload)
                source_retained = Path(temp, "staging", "durable.jpg").is_file()

        self.assertEqual(1, processed)
        self.assertEqual("READY", asset.status)
        self.assertEqual("COMPLETED", upload.status)
        self.assertTrue(asset.thumbnail_url)
        self.assertIn("preview", asset.variants_json)
        self.assertTrue(source_retained)

    def test_media_worker_automatically_retries_transient_storage_failures(self):
        asset = MediaAsset(
            owner_id=self.seller.id,
            media_type="image",
            status="PROCESSING",
            content_type="image/jpeg",
            file_size=128,
            storage_key="staging/transient.jpg",
            source_storage_key="staging/transient.jpg",
            source_url="https://storage.test/transient.jpg",
            original_url="https://storage.test/transient.jpg",
        )
        self.db.add(asset)
        self.db.flush()
        upload = UploadSession(
            media_asset_id=asset.id,
            owner_id=self.seller.id,
            status="PROCESSING",
            bytes_uploaded=128,
            total_bytes=128,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        self.db.add(upload)
        self.db.commit()

        with patch(
            "app.routers.platform_features.download_storage_object",
            side_effect=OSError("temporary object storage outage"),
        ):
            process_queued_media(self.db)
            self.db.refresh(asset)
            self.db.refresh(upload)
            self.assertEqual("PROCESSING", asset.status)
            self.assertEqual("PROCESSING", upload.status)
            self.assertEqual(1, asset.automatic_retry_count)
            self.assertEqual(0, asset.retry_count)
            self.assertIsNotNone(asset.processing_lease_until)

            asset.processing_lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
            self.db.commit()
            process_queued_media(self.db)
            asset.processing_lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
            self.db.commit()
            process_queued_media(self.db)
            self.db.refresh(asset)
            self.db.refresh(upload)

        self.assertEqual("FAILED", asset.status)
        self.assertEqual("PROCESSING_FAILED", upload.status)
        self.assertEqual(3, asset.automatic_retry_count)
        self.assertEqual(0, asset.retry_count)

        retried = retry_media_processing(asset.id, user=self.seller, db=self.db)
        self.assertEqual("PROCESSING", retried["status"])
        self.assertEqual(1, retried["retryCount"])
        self.assertEqual(0, retried["automaticRetryCount"])

    def test_media_worker_respects_active_lease_and_recovers_expired_lease(self):
        asset = MediaAsset(
            owner_id=self.seller.id,
            media_type="image",
            status="PROCESSING",
            content_type="image/jpeg",
            file_size=128,
            storage_key="staging/leased.jpg",
            source_storage_key="staging/leased.jpg",
            source_url="https://storage.test/leased.jpg",
            original_url="https://storage.test/leased.jpg",
            processing_lease_token="another-worker",
            processing_lease_until=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        self.db.add(asset)
        self.db.commit()

        with patch(
            "app.routers.platform_features.download_storage_object",
            side_effect=OSError("temporary outage"),
        ) as download:
            self.assertEqual(0, process_queued_media(self.db))
            download.assert_not_called()

            asset.processing_lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
            self.db.commit()
            process_queued_media(self.db)

        self.db.refresh(asset)
        self.assertEqual(1, asset.automatic_retry_count)
        self.assertNotEqual("another-worker", asset.processing_lease_token)

    def test_legacy_australian_phone_is_migrated_to_global_e164(self):
        legacy = User(
            id="legacy-au",
            heishi_id="HM-LEGACY-AU",
            nickname="Legacy",
            phone="0412345678",
            phone_verified=True,
            password_hash="x",
        )
        self.db.add(legacy)
        self.db.add(
            AuthIdentity(
                user_id=legacy.id,
                provider="phone",
                provider_subject="0412345678",
                verified=True,
            )
        )
        self.db.add(
            PhoneOtp(
                phone="0412345678",
                purpose="login",
                code_hash="x",
                expires_at=self.seller.created_at,
            )
        )
        self.db.commit()
        legacy_id = legacy.id
        self.db.close()
        run_migrations(self.engine)
        self.db = Session(self.engine)
        migrated = self.db.query(User).filter(User.id == legacy_id).one()
        identity = (
            self.db.query(AuthIdentity)
            .filter(AuthIdentity.user_id == legacy_id, AuthIdentity.provider == "phone")
            .one()
        )
        otp = self.db.query(PhoneOtp).filter(PhoneOtp.purpose == "login").one()
        self.assertEqual("+61412345678", migrated.phone)
        self.assertEqual("+61412345678", identity.provider_subject)
        self.assertEqual("+61412345678", otp.phone)
        self.assertEqual("+61412345678", normalize_phone("0412 345 678"))
        self.assertEqual("+14155552671", normalize_phone("+1 (415) 555-2671"))

    def test_exposure_rule_payload_contains_full_audit_fields(self):
        row = ExposureRule(
            product_id=99,
            rule_type="boost",
            exposure_weight=2,
            reason="Campaign",
            created_by=self.seller.id,
        )
        self.db.add(row)
        self.db.flush()
        payload = _exposure_payload(row)
        self.assertEqual(self.seller.id, payload["createdBy"])
        self.assertEqual("active", payload["status"])
        self.assertTrue(payload["createdAt"])
        self.assertTrue(payload["updatedAt"])

    def test_exposure_exclusion_blocks_interest_notifications(self):
        old_listing = Listing(
            seller_id=self.seller.id,
            type="product",
            title="Old camera",
            description="",
            price=10,
            category_key="digital",
            tag_key="",
            location_label="Melbourne",
            region_city="Melbourne",
            image_url="https://cdn.example/old.jpg",
            status="active",
            review_status="approved",
            created_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
        new_listing = Listing(
            seller_id=self.seller.id,
            type="product",
            title="New camera",
            description="",
            price=20,
            category_key="digital",
            tag_key="",
            location_label="Melbourne",
            region_city="Melbourne",
            image_url="https://cdn.example/new.jpg",
            status="active",
            review_status="approved",
        )
        self.buyer.city = "Melbourne"
        self.db.add_all([old_listing, new_listing])
        self.db.flush()
        self.db.add(Favorite(user_id=self.buyer.id, listing_id=old_listing.id))
        self.db.add(
            ExposureRule(
                product_id=new_listing.id,
                rule_type="exclude",
                exposure_weight=0,
                created_by=self.seller.id,
            )
        )
        self.db.commit()

        self.assertEqual(0, _process_interest_notifications(self.db))
        self.assertEqual(
            0,
            self.db.query(SystemNotification)
            .filter(
                SystemNotification.user_id == self.buyer.id,
                SystemNotification.notification_type == "interest_based_listing",
            )
            .count(),
        )

    def test_personalized_feed_boosts_followed_seller(self):
        other_seller = User(
            id="other-seller",
            heishi_id="HM-OTHER-SELLER",
            nickname="Other seller",
            password_hash="x",
            account_status="normal",
        )
        ordinary = Listing(
            seller_id=other_seller.id,
            type="product",
            title="Ordinary",
            description="",
            price=10,
            category_key="digital",
            tag_key="",
            location_label="Sydney",
            region_city="Sydney",
            image_url="https://cdn.example/ordinary.jpg",
            status="active",
            review_status="approved",
        )
        followed = Listing(
            seller_id=self.seller.id,
            type="product",
            title="Followed seller item",
            description="",
            price=10,
            category_key="misc",
            tag_key="",
            location_label="Melbourne",
            region_city="Melbourne",
            image_url="https://cdn.example/followed.jpg",
            status="active",
            review_status="approved",
        )
        self.db.add_all([other_seller, ordinary, followed])
        self.db.flush()
        self.db.add(Follow(follower_id=self.buyer.id, followed_id=self.seller.id))
        self.db.commit()

        rows = apply_feed_sort(
            self.db.query(Listing).filter(
                Listing.id.in_((ordinary.id, followed.id))
            ),
            viewer_user_id=self.buyer.id,
            viewer_city="Melbourne",
        ).all()
        self.assertEqual(followed.id, rows[0].id)

    def test_regional_exposure_targets_viewer_region_not_listing_region(self):
        ordinary = Listing(
            seller_id=self.seller.id,
            type="product",
            title="Ordinary Sydney item",
            description="",
            price=10,
            category_key="digital",
            tag_key="",
            location_label="Sydney",
            region_city="Sydney",
            image_url="https://cdn.example/ordinary-sydney.jpg",
            status="active",
            review_status="approved",
        )
        promoted = Listing(
            seller_id=self.seller.id,
            type="product",
            title="Melbourne item promoted to Sydney",
            description="",
            price=10,
            category_key="digital",
            tag_key="",
            location_label="Melbourne",
            region_city="Melbourne",
            image_url="https://cdn.example/promoted-melbourne.jpg",
            status="active",
            review_status="approved",
        )
        self.db.add_all([ordinary, promoted])
        self.db.flush()
        self.db.add(
            ExposureRule(
                product_id=promoted.id,
                rule_type="regional",
                exposure_weight=10,
                target_region="Sydney",
                created_by=self.seller.id,
            )
        )
        self.db.commit()

        rows = apply_feed_sort(
            self.db.query(Listing).filter(Listing.id.in_((ordinary.id, promoted.id))),
            viewer_city="Sydney",
        ).all()
        self.assertEqual(promoted.id, rows[0].id)

    def test_followed_category_drives_ranking_and_interest_notification(self):
        other_seller = User(
            id="category-other-seller",
            heishi_id="HM-CATEGORY-OTHER",
            nickname="Other seller",
            password_hash="x",
            account_status="normal",
        )
        followed_category_listing = Listing(
            seller_id=self.seller.id,
            type="product",
            title="Fresh digital item",
            description="",
            price=10,
            category_key="digital",
            tag_key="",
            location_label="Melbourne",
            region_city="Melbourne",
            image_url="https://cdn.example/digital.jpg",
            status="active",
            review_status="approved",
        )
        ordinary = Listing(
            seller_id=other_seller.id,
            type="product",
            title="Fresh miscellaneous item",
            description="",
            price=10,
            category_key="misc",
            tag_key="",
            location_label="Melbourne",
            region_city="Melbourne",
            image_url="https://cdn.example/misc.jpg",
            status="active",
            review_status="approved",
        )
        self.buyer.city = "Melbourne"
        self.db.add_all([other_seller, followed_category_listing, ordinary])
        self.db.flush()
        self.db.add(
            FollowedCategory(
                user_id=self.buyer.id,
                category_key="digital",
            )
        )
        self.db.commit()

        rows = apply_feed_sort(
            self.db.query(Listing).filter(
                Listing.id.in_((ordinary.id, followed_category_listing.id))
            ),
            viewer_user_id=self.buyer.id,
            viewer_city="Melbourne",
        ).all()
        self.assertEqual(followed_category_listing.id, rows[0].id)

        self.assertGreaterEqual(_process_interest_notifications(self.db), 1)
        self.assertEqual(
            1,
            self.db.query(SystemNotification)
            .filter(
                SystemNotification.user_id == self.buyer.id,
                SystemNotification.business_id == str(followed_category_listing.id),
                SystemNotification.notification_type == "interest_based_listing",
            )
            .count(),
        )

    def test_interest_notifications_exclude_suspended_seller(self):
        self.seller.account_status = "suspended"
        self.buyer.city = "Melbourne"
        listing = Listing(
            seller_id=self.seller.id,
            type="product",
            title="Unavailable seller item",
            description="",
            price=10,
            category_key="digital",
            tag_key="",
            location_label="Melbourne",
            region_city="Melbourne",
            image_url="https://cdn.example/suspended.jpg",
            status="active",
            review_status="approved",
        )
        self.db.add_all(
            [
                listing,
                FollowedCategory(user_id=self.buyer.id, category_key="digital"),
            ]
        )
        self.db.commit()

        self.assertEqual(0, _process_interest_notifications(self.db))
        self.assertEqual(
            0,
            self.db.query(SystemNotification)
            .filter(SystemNotification.user_id == self.buyer.id)
            .count(),
        )

    def test_suspended_seller_is_removed_from_all_public_catalog_queries(self):
        listing = Listing(
            seller_id=self.seller.id,
            type="product",
            title="Suspended catalog item",
            description="",
            price=10,
            category_key="digital",
            tag_key="",
            location_label="Melbourne",
            image_url="https://cdn.example/suspended-catalog.jpg",
            status="active",
            review_status="approved",
        )
        self.db.add(listing)
        self.db.commit()
        self.assertEqual(
            [listing.id],
            [
                row.id
                for row in apply_feed_listing_status_filter(
                    self.db.query(Listing), self.db, self.buyer.id
                ).all()
            ],
        )

        self.seller.account_status = "suspended"
        self.db.commit()
        self.assertEqual(
            [],
            apply_feed_listing_status_filter(
                self.db.query(Listing), self.db, self.buyer.id
            ).all(),
        )
        self.assertEqual(
            [],
            apply_public_listing_visibility_filter(self.db.query(Listing)).all(),
        )

    def test_admin_dispute_support_accepts_real_project_dispute_state(self):
        admin = User(
            id="admin",
            heishi_id="HM-ADMIN",
            nickname="Admin",
            password_hash="x",
            account_status="normal",
            is_admin=True,
        )
        order = Order(
            buyer_id=self.buyer.id,
            seller_id=self.seller.id,
            listing_id=1,
            amount=10,
            status="inDispute",
            dispute_status="open",
        )
        self.db.add_all([admin, order])
        self.db.commit()
        result = admin_open_support_conversation(
            AdminOpenConversationRequest(
                userId=self.seller.id,
                userRoleContext="seller",
                conversationType="DISPUTE_SUPPORT",
                orderId=order.id,
                subject="Dispute resolution",
                body="The administrator is reviewing this order.",
            ),
            admin=admin,
            db=self.db,
        )
        self.assertEqual("DISPUTE_SUPPORT", result["type"])
        self.assertEqual(order.id, result["orderId"])
        conversation = self.db.query(AdminConversation).one()
        self.assertEqual(self.seller.id, conversation.user_id)

    def test_admin_support_rejects_role_type_mismatch(self):
        admin = User(
            id="admin-mismatch",
            heishi_id="HM-ADMIN-MISMATCH",
            nickname="Admin",
            password_hash="x",
            account_status="normal",
            is_admin=True,
        )
        self.db.add(admin)
        self.db.commit()
        with self.assertRaises(HTTPException) as raised:
            admin_open_support_conversation(
                AdminOpenConversationRequest(
                    userId=self.seller.id,
                    userRoleContext="seller",
                    conversationType="BUYER_SUPPORT",
                    subject="Wrong role",
                    body="This must be rejected.",
                ),
                admin=admin,
                db=self.db,
            )
        self.assertEqual(409, raised.exception.status_code)
        self.assertEqual("SUPPORT_ROLE_MISMATCH", raised.exception.detail["code"])

    def test_admin_selected_group_announcements_expand_by_buyer_and_seller_role(self):
        admin = User(
            id="admin-announcement",
            heishi_id="HM-ADMIN-ANNOUNCEMENT",
            nickname="Admin",
            password_hash="x",
            account_status="normal",
            is_admin=True,
        )
        listing = Listing(
            seller_id=self.seller.id,
            title="Seller listing",
            price=10,
            category_key="misc",
            location_label="Melbourne",
            image_url="",
            status="active",
            review_status="approved",
        )
        self.db.add_all([admin, listing])
        self.db.commit()

        seller_result = create_role_announcement(
            AdminBroadcastRequest(
                audienceRole="seller",
                userIds=[self.seller.id, self.buyer.id],
                title="Seller notice",
                body="Selected sellers only",
            ),
            admin=admin,
            db=self.db,
        )
        self.assertEqual(1, seller_result["recipientCount"])
        seller_notifications = (
            self.db.query(SystemNotification)
            .filter(SystemNotification.business_id == seller_result["announcementId"])
            .all()
        )
        self.assertEqual(
            [(self.seller.id, "seller")],
            [(row.user_id, row.user_role_context) for row in seller_notifications],
        )

        buyer_result = create_role_announcement(
            AdminBroadcastRequest(
                audienceRole="buyer",
                userIds=[self.seller.id, self.buyer.id],
                title="Buyer notice",
                body="Selected buyers only",
            ),
            admin=admin,
            db=self.db,
        )
        self.assertEqual(2, buyer_result["recipientCount"])
        buyer_notifications = (
            self.db.query(SystemNotification)
            .filter(SystemNotification.business_id == buyer_result["announcementId"])
            .order_by(SystemNotification.user_id)
            .all()
        )
        self.assertEqual(
            sorted([(self.seller.id, "buyer"), (self.buyer.id, "buyer")]),
            [(row.user_id, row.user_role_context) for row in buyer_notifications],
        )

    def test_historical_merge_deduplicates_user_library_without_losing_state(self):
        admin = User(
            id="admin-historical-merge",
            heishi_id="HM-ADMIN-HISTORICAL",
            nickname="Admin",
            password_hash="x",
            is_admin=True,
        )
        duplicate = User(
            id="historical-duplicate",
            heishi_id="HM-HISTORICAL-DUP",
            nickname="Duplicate",
            password_hash="x",
            account_status="normal",
        )
        listing = Listing(
            seller_id=self.seller.id,
            title="Shared favorite",
            price=10,
            category_key="misc",
            location_label="Melbourne",
            image_url="",
            status="active",
            review_status="approved",
            favorite_count=2,
        )
        self.db.add_all([admin, duplicate, listing])
        self.db.flush()
        self.db.add_all(
            [
                Favorite(user_id=self.buyer.id, listing_id=listing.id),
                Favorite(user_id=duplicate.id, listing_id=listing.id),
                NotificationPreference(
                    user_id=self.buyer.id,
                    user_role_context="buyer",
                    category="marketing",
                    push_enabled=False,
                ),
                NotificationPreference(
                    user_id=duplicate.id,
                    user_role_context="buyer",
                    category="marketing",
                    push_enabled=True,
                ),
                UserSettings(user_id=duplicate.id, personalization=False),
                DevicePushToken(
                    user_id=duplicate.id,
                    token="retired-device-push-token",
                    platform="android",
                ),
            ]
        )
        self.db.commit()

        result = merge_user_accounts(
            AdminMergeAccountsRequest(
                sourceUserId=duplicate.id,
                destinationUserId=self.buyer.id,
            ),
            db=self.db,
            admin=admin,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(
            1,
            self.db.query(Favorite)
            .filter(Favorite.user_id == self.buyer.id, Favorite.listing_id == listing.id)
            .count(),
        )
        self.db.refresh(listing)
        self.assertEqual(1, listing.favorite_count)
        preference = self.db.query(NotificationPreference).filter(
            NotificationPreference.user_id == self.buyer.id,
            NotificationPreference.user_role_context == "buyer",
            NotificationPreference.category == "marketing",
        ).one()
        self.assertFalse(preference.push_enabled)
        self.assertEqual(
            self.buyer.id,
            self.db.query(UserSettings).filter(UserSettings.personalization.is_(False)).one().user_id,
        )
        self.assertEqual(
            0,
            self.db.query(DevicePushToken)
            .filter(DevicePushToken.token == "retired-device-push-token")
            .count(),
        )

    def _share_fixture(self, token: str) -> tuple[Listing, ShareRecord]:
        listing = Listing(
            seller_id=self.seller.id,
            title="Shared listing",
            price=10,
            category_key="misc",
            location_label="Melbourne",
            image_url="",
            status="active",
            review_status="approved",
        )
        self.db.add(listing)
        self.db.flush()
        share = ShareRecord(
            share_token=token,
            product_id=listing.id,
            sharer_user_id=self.seller.id,
            status="active",
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        )
        self.db.add(share)
        self.db.commit()
        return listing, share

    def test_share_attribution_respects_denied_consent_for_authenticated_user(self):
        _listing, share = self._share_fixture("denied-consent-token")
        anonymous = AnonymousSession(
            consent_status="denied",
            linked_user_id=self.buyer.id,
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        )
        self.db.add(anonymous)
        self.db.commit()
        result = record_share_event(
            share.share_token,
            ShareEventRequest(eventType="registration", anonymousSessionId=anonymous.id),
            user=self.buyer,
            db=self.db,
        )
        self.assertFalse(result["recorded"])
        self.assertEqual("analytics_consent_required", result["reason"])
        self.assertEqual(0, self.db.query(ShareAttributionEvent).count())

    def test_share_attribution_rejects_guest_reuse_of_linked_session(self):
        _listing, share = self._share_fixture("linked-session-token")
        anonymous = AnonymousSession(
            consent_status="granted",
            linked_user_id=self.buyer.id,
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        )
        self.db.add(anonymous)
        self.db.commit()
        with self.assertRaises(HTTPException) as raised:
            record_share_event(
                share.share_token,
                ShareEventRequest(eventType="open", anonymousSessionId=anonymous.id),
                user=None,
                db=self.db,
            )
        self.assertEqual("SESSION_ALREADY_LINKED", raised.exception.detail["code"])

    def test_share_attribution_rejects_unlinked_session_after_authentication(self):
        _listing, share = self._share_fixture("unlinked-session-token")
        anonymous = AnonymousSession(
            consent_status="granted",
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        )
        self.db.add(anonymous)
        self.db.commit()
        with self.assertRaises(HTTPException) as raised:
            record_share_event(
                share.share_token,
                ShareEventRequest(eventType="view", anonymousSessionId=anonymous.id),
                user=self.buyer,
                db=self.db,
            )
        self.assertEqual("SESSION_NOT_LINKED", raised.exception.detail["code"])

    def test_share_attribution_respects_authenticated_personalization_opt_out(self):
        _listing, share = self._share_fixture("privacy-setting-token")
        anonymous = AnonymousSession(
            consent_status="granted",
            linked_user_id=self.buyer.id,
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        )
        self.db.add_all(
            [anonymous, UserSettings(user_id=self.buyer.id, personalization=False)]
        )
        self.db.commit()
        result = record_share_event(
            share.share_token,
            ShareEventRequest(eventType="view", anonymousSessionId=anonymous.id),
            user=self.buyer,
            db=self.db,
        )
        self.assertFalse(result["recorded"])
        self.assertEqual("personalization_disabled", result["reason"])
        self.assertEqual(0, self.db.query(ShareAttributionEvent).count())

    def test_share_attribution_records_owned_consented_session(self):
        _listing, share = self._share_fixture("consented-session-token")
        anonymous = AnonymousSession(
            consent_status="granted",
            linked_user_id=self.buyer.id,
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        )
        self.db.add_all(
            [anonymous, UserSettings(user_id=self.buyer.id, personalization=True)]
        )
        self.db.commit()
        result = record_share_event(
            share.share_token,
            ShareEventRequest(eventType="view", anonymousSessionId=anonymous.id),
            user=self.buyer,
            db=self.db,
        )
        self.assertTrue(result["accepted"])
        event = self.db.query(ShareAttributionEvent).one()
        self.assertEqual(self.buyer.id, event.user_id)
        self.assertEqual(anonymous.id, event.anonymous_session_id)

    def test_personalization_opt_out_removes_interest_identity_from_feed(self):
        self.db.add(UserSettings(user_id=self.buyer.id, personalization=False))
        self.db.commit()
        self.assertIsNone(_personalized_viewer_id(self.db, self.buyer))

    def test_personalization_opt_out_blocks_interest_notifications(self):
        prior = Listing(
            seller_id=self.seller.id,
            title="Previously viewed camera",
            description="",
            price=10,
            category_key="digital",
            location_label="Melbourne",
            region_city="Melbourne",
            image_url="https://cdn.example/prior.jpg",
            status="inactive",
            review_status="approved",
            created_at=datetime.now(timezone.utc) - timedelta(days=2),
        )
        candidate = Listing(
            seller_id=self.seller.id,
            title="New camera",
            description="",
            price=20,
            category_key="digital",
            location_label="Melbourne",
            region_city="Melbourne",
            image_url="https://cdn.example/new-optout.jpg",
            status="active",
            review_status="approved",
        )
        self.buyer.city = "Melbourne"
        self.db.add_all([prior, candidate])
        self.db.flush()
        self.db.add_all(
            [
                Favorite(user_id=self.buyer.id, listing_id=prior.id),
                UserSettings(user_id=self.buyer.id, personalization=False),
            ]
        )
        self.db.commit()
        self.assertEqual(0, _process_interest_notifications(self.db))
        self.assertEqual(
            0,
            self.db.query(SystemNotification)
            .filter(SystemNotification.user_id == self.buyer.id)
            .count(),
        )

    def test_non_normal_accounts_have_no_guest_public_profile_or_catalog(self):
        self.seller.account_status = "suspended"
        self.db.commit()
        request = Request({"type": "http", "headers": []})
        with self.assertRaises(HTTPException) as profile_error:
            get_public_profile(self.seller.id, request, self.db)
        self.assertEqual(404, profile_error.exception.status_code)
        with self.assertRaises(HTTPException) as listings_error:
            get_public_listings(self.seller.id, request, db=self.db)
        self.assertEqual(404, listings_error.exception.status_code)

    def test_media_checksum_deduplication_has_database_invariant(self):
        checksum = "a" * 64
        first = MediaAsset(
            owner_id=self.seller.id,
            media_type="image",
            content_type="image/jpeg",
            checksum_sha256=checksum,
            deduplication_key=f"{self.seller.id}:{checksum}",
            storage_key="dedup/first.jpg",
        )
        second = MediaAsset(
            owner_id=self.seller.id,
            media_type="image",
            content_type="image/jpeg",
            checksum_sha256=checksum,
            deduplication_key=f"{self.seller.id}:{checksum}",
            storage_key="dedup/second.jpg",
        )
        self.db.add(first)
        self.db.commit()
        self.db.add(second)
        with self.assertRaises(IntegrityError):
            self.db.commit()
        self.db.rollback()

    def test_share_attribution_deduplication_has_database_invariant(self):
        _listing, share = self._share_fixture("database-dedup-share-token")
        key = "b" * 64
        first = ShareAttributionEvent(
            share_id=share.id,
            user_id=self.buyer.id,
            event_type="view",
            deduplication_key=key,
        )
        second = ShareAttributionEvent(
            share_id=share.id,
            user_id=self.buyer.id,
            event_type="view",
            deduplication_key=key,
        )
        self.db.add(first)
        self.db.commit()
        self.db.add(second)
        with self.assertRaises(IntegrityError):
            self.db.commit()
        self.db.rollback()

    def test_legacy_deduplication_migration_enforces_upgrade_invariants(self):
        database_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        database_file.close()
        self.addCleanup(lambda: Path(database_file.name).unlink(missing_ok=True))
        engine = create_engine(f"sqlite:///{database_file.name}")
        self.addCleanup(engine.dispose)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE notification_dispatches ("
                    "id VARCHAR(36) PRIMARY KEY, deduplication_key VARCHAR(255))"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO notification_dispatches (id, deduplication_key) VALUES "
                    "('dispatch-1', 'same-key'), ('dispatch-2', 'same-key')"
                )
            )
            connection.execute(
                text(
                    "CREATE TABLE share_attribution_events ("
                    "id VARCHAR(36) PRIMARY KEY, share_id VARCHAR(36), "
                    "event_type VARCHAR(30), user_id VARCHAR(36), "
                    "anonymous_session_id VARCHAR(36), business_id VARCHAR(50), "
                    "deduplication_key VARCHAR(64), created_at DATETIME)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO share_attribution_events "
                    "(id, share_id, event_type, user_id, created_at) VALUES "
                    "('event-1', 'share-1', 'view', 'user-1', CURRENT_TIMESTAMP)"
                )
            )

        _backfill_expanded_deduplication_keys(engine)

        with engine.begin() as connection:
            count = connection.execute(
                text("SELECT COUNT(*) FROM notification_dispatches")
            ).scalar_one()
            self.assertEqual(1, count)
            with self.assertRaises(IntegrityError):
                connection.execute(
                    text(
                        "INSERT INTO notification_dispatches (id, deduplication_key) "
                        "VALUES ('dispatch-3', 'same-key')"
                    )
                )

        with engine.begin() as connection:
            key = connection.execute(
                text(
                    "SELECT deduplication_key FROM share_attribution_events "
                    "WHERE id='event-1'"
                )
            ).scalar_one()
            self.assertEqual(64, len(key))
            with self.assertRaises(IntegrityError):
                connection.execute(
                    text(
                        "INSERT INTO share_attribution_events "
                        "(id, share_id, event_type, created_at) VALUES "
                        "('event-2', 'share-2', 'view', CURRENT_TIMESTAMP)"
                    )
                )

    @unittest.skipUnless(video_processor_available(), "FFmpeg runtime is not installed")
    def test_video_pipeline_generates_adaptive_hls_and_mp4_variants(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.mp4"
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc=size=320x240:rate=12",
                    "-t",
                    "1",
                    "-pix_fmt",
                    "yuv420p",
                    str(source),
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=60,
            )
            processed = process_video_variants(source.read_bytes())
        self.assertIn("preview", processed.variants)
        self.assertIn("standard", processed.variants)
        self.assertIn("master.m3u8", processed.adaptive_files)
        self.assertIn("preview.m3u8", processed.adaptive_files)
        self.assertTrue(any(name.endswith(".ts") for name in processed.adaptive_files))


if __name__ == "__main__":
    unittest.main()
