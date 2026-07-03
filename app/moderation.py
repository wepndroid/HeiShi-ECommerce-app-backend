"""Content moderation helpers."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import BlockedKeyword

DEFAULT_BLOCKED_KEYWORDS = (
    "counterfeit",
    "replica",
    "essay writing",
    "currency exchange",
    "firearm",
    "weapon",
)

def seed_blocked_keywords(db: Session) -> None:
    for pattern in DEFAULT_BLOCKED_KEYWORDS:
        if db.query(BlockedKeyword).filter(BlockedKeyword.pattern == pattern).first():
            continue
        db.add(BlockedKeyword(pattern=pattern, locale="all", active=True))
    db.commit()

def find_blocked_keyword(db: Session, *texts: str) -> str | None:
    haystack = " ".join(t for t in texts if t).lower()
    if not haystack.strip():
        return None
    for row in db.query(BlockedKeyword).filter(BlockedKeyword.active.is_(True)).all():
        if row.pattern.lower() in haystack:
            return row.pattern
    return None
