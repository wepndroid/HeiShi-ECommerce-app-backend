from sqlalchemy import and_, or_
from sqlalchemy.orm import Query, Session

from app.models import BlocklistEntry, Listing


def users_blocked(db: Session, user_a: str, user_b: str) -> bool:
    if not user_a or not user_b or user_a == user_b:
        return False
    row = (
        db.query(BlocklistEntry.id)
        .filter(
            or_(
                and_(BlocklistEntry.blocker_id == user_a, BlocklistEntry.blocked_id == user_b),
                and_(BlocklistEntry.blocker_id == user_b, BlocklistEntry.blocked_id == user_a),
            )
        )
        .first()
    )
    return row is not None


def blocked_user_ids(db: Session, viewer_id: str) -> set[str]:
    rows = db.query(BlocklistEntry).filter(
        or_(BlocklistEntry.blocker_id == viewer_id, BlocklistEntry.blocked_id == viewer_id)
    ).all()
    out: set[str] = set()
    for row in rows:
        other = row.blocked_id if row.blocker_id == viewer_id else row.blocker_id
        out.add(other)
    return out


def exclude_blocked_sellers(query: Query, db: Session, viewer_id: str | None) -> Query:
    if not viewer_id:
        return query
    blocked = blocked_user_ids(db, viewer_id)
    if not blocked:
        return query
    return query.filter(Listing.seller_id.notin_(blocked))
