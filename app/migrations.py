"""Lightweight SQLite column migrations for existing dev databases."""

from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine


def _sqlite_add_column_if_missing(engine: Engine, table: str, column: str, ddl: str) -> bool:
    inspector = inspect(engine)
    if table not in inspector.get_table_names():
        return False
    existing = {col["name"] for col in inspector.get_columns(table)}
    if column in existing:
        return False
    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))
    return True


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

    _sqlite_add_column_if_missing(engine, "users", "email", "email VARCHAR(255)")
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

    # Stripe payments drop-in: buyer Customer + saved-card metadata + Connect payout status.
    _sqlite_add_column_if_missing(engine, "users", "stripe_customer_id", "stripe_customer_id VARCHAR(100)")
    for col, ddl in (
        ("stripe_payment_method_id", "stripe_payment_method_id VARCHAR(100)"),
        ("brand", "brand VARCHAR(20)"),
        ("exp_month", "exp_month INTEGER"),
        ("exp_year", "exp_year INTEGER"),
    ):
        _sqlite_add_column_if_missing(engine, "payment_methods", col, ddl)
    for col, ddl in (
        ("account_ref", "account_ref VARCHAR(255)"),
        ("stripe_external_account_id", "stripe_external_account_id VARCHAR(100)"),
        ("payouts_enabled", "payouts_enabled BOOLEAN DEFAULT 0"),
        ("paypal_merchant_id", "paypal_merchant_id VARCHAR(32)"),
        ("paypal_tracking_id", "paypal_tracking_id VARCHAR(127)"),
        ("paypal_permissions_granted", "paypal_permissions_granted BOOLEAN DEFAULT 0"),
        ("paypal_email_confirmed", "paypal_email_confirmed BOOLEAN DEFAULT 0"),
    ):
        _sqlite_add_column_if_missing(engine, "payout_methods", col, ddl)
    _sqlite_add_column_if_missing(engine, "orders", "paypal_payee_merchant_id", "paypal_payee_merchant_id VARCHAR(32)")
    _sqlite_add_column_if_missing(engine, "orders", "paypal_disbursement_mode", "paypal_disbursement_mode VARCHAR(20)")

    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_conversation_listing_parties "
                "ON conversations (listing_id, buyer_id, seller_id)"
            )
        )

    _sqlite_migrate_mvp_admin(engine)
    _sqlite_migrate_mvp_admin_v2(engine)
    _sqlite_migrate_escrow_fee_defaults(engine)


def _sqlite_migrate_escrow_fee_defaults(engine: Engine) -> None:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        if "orders" in tables:
            conn.execute(text("UPDATE orders SET escrow_fee = 0 WHERE escrow_fee IS NULL OR escrow_fee != 0"))
        if "platform_settings" in tables:
            conn.execute(
                text(
                    """
                    INSERT INTO platform_settings (key, value, updated_at)
                    SELECT 'payments.escrowFee', '0', CURRENT_TIMESTAMP
                    WHERE NOT EXISTS (
                        SELECT 1 FROM platform_settings WHERE key = 'payments.escrowFee'
                    )
                    """
                )
            )


def _sqlite_migrate_mvp_admin_v2(engine: Engine) -> None:
    """Admin MVP alignment: user moderation controls, category display, review moderation.

    New tables (report_reasons, product_tags, search_logs, platform_settings) are created
    automatically by Base.metadata.create_all; only added columns need backfilling here.
    """

    user_cols = (
        ("is_muted", "is_muted BOOLEAN DEFAULT 0"),
        ("muted_at", "muted_at DATETIME"),
        ("mute_reason", "mute_reason TEXT"),
        ("publish_restricted", "publish_restricted BOOLEAN DEFAULT 0"),
        ("publish_restricted_at", "publish_restricted_at DATETIME"),
        ("publish_restrict_reason", "publish_restrict_reason TEXT"),
        ("is_flagged", "is_flagged BOOLEAN DEFAULT 0"),
        ("flag_reason", "flag_reason TEXT"),
        ("email_verified", "email_verified BOOLEAN DEFAULT 0"),
        ("google_sub", "google_sub VARCHAR(100)"),
    )
    for col, ddl in user_cols:
        _sqlite_add_column_if_missing(engine, "users", col, ddl)

    for col, ddl in (
        ("icon", "icon VARCHAR(50)"),
        ("show_on_home", "show_on_home BOOLEAN DEFAULT 1"),
    ):
        _sqlite_add_column_if_missing(engine, "platform_categories", col, ddl)

    for col, ddl in (
        ("is_hidden", "is_hidden BOOLEAN DEFAULT 0"),
        ("is_removed", "is_removed BOOLEAN DEFAULT 0"),
        ("admin_note", "admin_note TEXT"),
    ):
        _sqlite_add_column_if_missing(engine, "reviews", col, ddl)


def _sqlite_migrate_mvp_admin(engine: Engine) -> None:
    """PROG-401: columns for admin, moderation, payments, verification."""

    user_cols = (
        ("is_admin", "is_admin BOOLEAN DEFAULT 0"),
        ("account_status", "account_status VARCHAR(20) DEFAULT 'normal'"),
        ("admin_notes", "admin_notes TEXT"),
        ("banned_at", "banned_at DATETIME"),
        ("ban_reason", "ban_reason TEXT"),
        ("stripe_connect_id", "stripe_connect_id VARCHAR(100)"),
        ("wechat_openid", "wechat_openid VARCHAR(100)"),
        ("wechat_unionid", "wechat_unionid VARCHAR(100)"),
        ("preferred_display_currency", "preferred_display_currency VARCHAR(3) DEFAULT 'aud'"),
    )
    for col, ddl in user_cols:
        _sqlite_add_column_if_missing(engine, "users", col, ddl)

    review_status_added = _sqlite_add_column_if_missing(
        engine, "listings", "review_status", "review_status VARCHAR(20) DEFAULT 'pendingReview'"
    )
    listing_cols = (
        ("review_note", "review_note TEXT"),
        ("reviewed_at", "reviewed_at DATETIME"),
        ("reviewed_by", "reviewed_by VARCHAR(36)"),
        ("is_recommended", "is_recommended BOOLEAN DEFAULT 0"),
        ("is_pinned", "is_pinned BOOLEAN DEFAULT 0"),
        ("promotion_click_count", "promotion_click_count INTEGER DEFAULT 0"),
    )
    for col, ddl in listing_cols:
        _sqlite_add_column_if_missing(engine, "listings", col, ddl)

    order_cols = (
        ("payment_method", "payment_method VARCHAR(30)"),
        ("psp", "psp VARCHAR(20)"),
        ("payment_status", "payment_status VARCHAR(30)"),
        ("psp_payment_id", "psp_payment_id VARCHAR(100)"),
        ("psp_transaction_id", "psp_transaction_id VARCHAR(100)"),
        ("charge_currency", "charge_currency VARCHAR(3) DEFAULT 'aud'"),
        ("amount_minor", "amount_minor INTEGER"),
        ("display_amount_cny", "display_amount_cny FLOAT"),
        ("payout_paused", "payout_paused BOOLEAN DEFAULT 0"),
        ("payout_status", "payout_status VARCHAR(30) DEFAULT 'pending'"),
        ("payout_provider", "payout_provider VARCHAR(20)"),
        ("payout_method_id", "payout_method_id VARCHAR(36)"),
        ("payout_reference", "payout_reference VARCHAR(100)"),
        ("payout_failure_code", "payout_failure_code VARCHAR(50)"),
        ("payout_failure_reason", "payout_failure_reason TEXT"),
        ("payout_released_at", "payout_released_at DATETIME"),
        ("payout_failed_at", "payout_failed_at DATETIME"),
        ("payout_reversed_at", "payout_reversed_at DATETIME"),
        ("payout_reversal_reference", "payout_reversal_reference VARCHAR(100)"),
        ("is_abnormal", "is_abnormal BOOLEAN DEFAULT 0"),
        ("admin_notes", "admin_notes TEXT"),
        ("dispute_status", "dispute_status VARCHAR(30)"),
        ("dispute_reason", "dispute_reason TEXT"),
        ("dispute_evidence_json", "dispute_evidence_json TEXT DEFAULT '[]'"),
        ("confirmed_at", "confirmed_at DATETIME"),
        ("auto_confirm_at", "auto_confirm_at DATETIME"),
    )
    for col, ddl in order_cols:
        _sqlite_add_column_if_missing(engine, "orders", col, ddl)

    report_cols = (
        ("evidence_urls_json", "evidence_urls_json TEXT DEFAULT '[]'"),
        ("handler_note", "handler_note TEXT"),
        ("handled_by", "handled_by VARCHAR(36)"),
        ("handled_at", "handled_at DATETIME"),
    )
    for col, ddl in report_cols:
        _sqlite_add_column_if_missing(engine, "safety_reports", col, ddl)

    if review_status_added:
        with engine.begin() as conn:
            conn.execute(text("UPDATE listings SET review_status = 'approved'"))
