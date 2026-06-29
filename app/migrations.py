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


def _sqlite_reviews_table_corrupted(engine: Engine) -> bool:
    inspector = inspect(engine)
    if "reviews" not in inspector.get_table_names():
        return False
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT comment, rating FROM reviews "
                "WHERE comment IS NOT NULL OR rating IS NOT NULL LIMIT 1"
            )
        ).fetchone()
    if not row:
        return False
    comment, rating = row
    if comment is not None and str(comment).startswith("202"):
        return True
    try:
        int(rating)
    except (TypeError, ValueError):
        return True
    return False


def _sqlite_create_reviews_table(conn) -> None:
    conn.execute(text("DROP TABLE IF EXISTS reviews"))
    conn.execute(
        text(
            """
            CREATE TABLE reviews (
                id VARCHAR(36) PRIMARY KEY,
                order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
                reviewer_id VARCHAR(36) NOT NULL REFERENCES users(id),
                rating INTEGER NOT NULL,
                comment TEXT,
                quality_rating INTEGER,
                communication_rating INTEGER,
                expertise_rating INTEGER,
                professionalism_rating INTEGER,
                hire_again_rating INTEGER,
                created_at DATETIME,
                UNIQUE (order_id, reviewer_id)
            )
            """
        )
    )
    conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_reviews_order_reviewer "
            "ON reviews (order_id, reviewer_id)"
        )
    )


def _sqlite_repair_reviews_corruption(engine: Engine) -> None:
    if not _sqlite_reviews_table_corrupted(engine):
        return
    with engine.begin() as conn:
        _sqlite_create_reviews_table(conn)


def _sqlite_migrate_reviews_dual_party(engine: Engine) -> None:
    """Allow buyer + seller each to review the same order (drop single-review unique on order_id)."""
    _sqlite_repair_reviews_corruption(engine)

    inspector = inspect(engine)
    if "reviews" not in inspector.get_table_names():
        return
    indexes = {idx["name"] for idx in inspector.get_indexes("reviews")}
    if "uq_reviews_order_reviewer" in indexes:
        return

    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE reviews_new (
                    id VARCHAR(36) PRIMARY KEY,
                    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
                    reviewer_id VARCHAR(36) NOT NULL REFERENCES users(id),
                    rating INTEGER NOT NULL,
                    comment TEXT,
                    quality_rating INTEGER,
                    communication_rating INTEGER,
                    expertise_rating INTEGER,
                    professionalism_rating INTEGER,
                    hire_again_rating INTEGER,
                    created_at DATETIME,
                    UNIQUE (order_id, reviewer_id)
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO reviews_new (
                    id, order_id, reviewer_id, rating, comment,
                    quality_rating, communication_rating, expertise_rating,
                    professionalism_rating, hire_again_rating, created_at
                )
                SELECT
                    id, order_id, reviewer_id, rating, comment,
                    quality_rating, communication_rating, expertise_rating,
                    professionalism_rating, hire_again_rating, created_at
                FROM reviews
                """
            )
        )
        conn.execute(text("DROP TABLE reviews"))
        conn.execute(text("ALTER TABLE reviews_new RENAME TO reviews"))
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_reviews_order_reviewer "
                "ON reviews (order_id, reviewer_id)"
            )
        )


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

    for col in (
        "quality_rating",
        "communication_rating",
        "expertise_rating",
        "professionalism_rating",
        "hire_again_rating",
    ):
        _sqlite_add_column_if_missing(engine, "reviews", col, f"{col} INTEGER")

    _sqlite_migrate_reviews_dual_party(engine)

    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_conversation_listing_parties "
                "ON conversations (listing_id, buyer_id, seller_id)"
            )
        )