import hashlib
import io
import unittest

from fastapi import HTTPException
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.media_processing import MediaValidationError, process_image_variants
from app.models import Conversation, Listing, Order, User
from app.routers.platform_features import (
    CreateOfferRequest,
    PreferenceUpdate,
    accept_private_offer,
    create_private_offer,
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
        self.assertEqual(self.listing.price, 100)

    def test_private_offer_cannot_raise_public_price(self):
        with self.assertRaises(HTTPException) as invalid:
            create_private_offer(
                self.conversation.id,
                CreateOfferRequest(negotiatedPrice=101),
                self.seller,
                self.db,
            )
        self.assertEqual(invalid.exception.status_code, 422)

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


if __name__ == "__main__":
    unittest.main()
