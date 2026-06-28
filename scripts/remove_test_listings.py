"""Remove Sprint6 verify and asdf test listings from the local database."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import Conversation, Favorite, Listing, Message, Order, Review, ViewHistory


def listing_filter():
    return or_(
        Listing.title.like("Sprint6 verify%"),
        Listing.title == "asdf",
    )


def purge_listing(db: Session, listing: Listing) -> None:
    listing_id = listing.id
    conv_ids = [row[0] for row in db.query(Conversation.id).filter(Conversation.listing_id == listing_id).all()]
    if conv_ids:
        db.query(Message).filter(Message.conversation_id.in_(conv_ids)).delete(synchronize_session=False)
        db.query(Conversation).filter(Conversation.id.in_(conv_ids)).delete(synchronize_session=False)

    order_ids = [row[0] for row in db.query(Order.id).filter(Order.listing_id == listing_id).all()]
    if order_ids:
        db.query(Review).filter(Review.order_id.in_(order_ids)).delete(synchronize_session=False)
        db.query(Order).filter(Order.id.in_(order_ids)).delete(synchronize_session=False)

    db.query(Favorite).filter(Favorite.listing_id == listing_id).delete(synchronize_session=False)
    db.query(ViewHistory).filter(ViewHistory.listing_id == listing_id).delete(synchronize_session=False)
    db.delete(listing)


def main() -> None:
    db = SessionLocal()
    try:
        targets = db.query(Listing).filter(listing_filter()).order_by(Listing.id).all()
        if not targets:
            print("No matching listings found.")
            return
        print(f"Removing {len(targets)} listing(s):")
        for listing in targets:
            print(f"  - id={listing.id} type={listing.type} title={listing.title!r} status={listing.status}")
            purge_listing(db, listing)
        db.commit()
        remaining = db.query(Listing).filter(listing_filter()).count()
        print(f"Done. Remaining matches: {remaining}")
    finally:
        db.close()


if __name__ == "__main__":
    main()