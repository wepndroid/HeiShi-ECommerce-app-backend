import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.catalog_helpers import apply_feed_listing_status_filter
from app.database import Base
from app.models import Listing, User


class CatalogFeedVisibilityTests(unittest.TestCase):
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
        self.db.add(self.seller)
        self.db.flush()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def add_listing(self, title: str, status: str, review_status: str) -> Listing:
        listing = Listing(
            seller_id=self.seller.id,
            title=title,
            description="test",
            price=10,
            category_key="home",
            location_label="Melbourne",
            image_url="https://example.com/test.jpg",
            status=status,
            review_status=review_status,
        )
        self.db.add(listing)
        self.db.flush()
        return listing

    def test_feed_includes_only_active_approved_listings_for_every_viewer(self):
        active = self.add_listing("Active", "active", "approved")
        self.add_listing("Sold", "sold", "approved")
        self.add_listing("Inactive", "inactive", "approved")
        self.add_listing("Pending review", "active", "pendingReview")

        for viewer_id in (None, "buyer", self.seller.id):
            with self.subTest(viewer_id=viewer_id):
                rows = apply_feed_listing_status_filter(
                    self.db.query(Listing), self.db, viewer_id
                ).all()
                self.assertEqual([row.id for row in rows], [active.id])


if __name__ == "__main__":
    unittest.main()
