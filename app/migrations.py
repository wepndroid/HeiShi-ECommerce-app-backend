"""Lightweight SQLite column migrations for existing dev databases."""

from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine


def _sqlite_add_column_if_missing(engine: Engine, table: str, column: str, ddl: str) -> None:
    inspector = inspect(engine)
    if table not in inspector.get_table_names():
        return
    existing = {col["name"] for col in inspector.get_columns(table)}
    if column in existing:
        return
    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))


def run_migrations(engine: Engine) -> None:
    if not str(engine.url).startswith("sqlite"):
        return

    _sqlite_add_column_if_missing(engine, "system_notifications", "category", "category VARCHAR(20) DEFAULT 'system'")
    _sqlite_add_column_if_missing(engine, "system_notifications", "title_zh", "title_zh VARCHAR(200)")
    _sqlite_add_column_if_missing(engine, "system_notifications", "body_zh", "body_zh TEXT")
    _sqlite_add_column_if_missing(engine, "system_notifications", "action_type", "action_type VARCHAR(30)")
    _sqlite_add_column_if_missing(engine, "system_notifications", "action_ref", "action_ref VARCHAR(50)")
    _sqlite_add_column_if_missing(engine, "listings", "bundle_meta_json", "bundle_meta_json TEXT DEFAULT '{}'")

    _sqlite_add_column_if_missing(
        engine, "conversations", "buyer_read_inbox_at", "buyer_read_inbox_at DATETIME"
    )
    _sqlite_add_column_if_missing(
        engine, "conversations", "seller_read_inbox_at", "seller_read_inbox_at DATETIME"
    )
    _sqlite_add_column_if_missing(
        engine, "conversations", "buyer_marked_unread", "buyer_marked_unread BOOLEAN DEFAULT 0"
    )
    _sqlite_add_column_if_missing(
        engine, "conversations", "seller_marked_unread", "seller_marked_unread BOOLEAN DEFAULT 0"
    )
    _sqlite_add_column_if_missing(engine, "coupons", "kind", "kind VARCHAR(30)")
    _sqlite_add_column_if_missing(engine, "orders", "bundle_item_id", "bundle_item_id VARCHAR(36)")
    _sqlite_add_column_if_missing(engine, "orders", "coupon_id", "coupon_id VARCHAR(36)")
    _sqlite_add_column_if_missing(engine, "orders", "discount_amount", "discount_amount FLOAT DEFAULT 0")
    _sqlite_add_column_if_missing(engine, "listings", "meet_in_public", "meet_in_public BOOLEAN DEFAULT 1")
    _sqlite_add_column_if_missing(engine, "user_settings", "remind_pay", "remind_pay BOOLEAN DEFAULT 1")
    _sqlite_add_column_if_missing(engine, "user_settings", "remind_ship", "remind_ship BOOLEAN DEFAULT 1")
    _sqlite_add_column_if_missing(engine, "user_settings", "remind_receive", "remind_receive BOOLEAN DEFAULT 1")
    _sqlite_add_column_if_missing(engine, "user_settings", "remind_dispute", "remind_dispute BOOLEAN DEFAULT 1")

    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_conversation_listing_parties "
                "ON conversations (listing_id, buyer_id, seller_id)"
            )
        )