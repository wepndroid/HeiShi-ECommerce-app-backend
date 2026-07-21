"""Lightweight SQLite column migrations for existing dev databases."""

from __future__ import annotations

import hashlib

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


def _postgres_add_column_if_missing(
    engine: Engine, table: str, column: str, ddl: str
) -> bool:
    """Add one known application column to an existing PostgreSQL table."""
    inspector = inspect(engine)
    if table not in inspector.get_table_names():
        return False
    existing = {col["name"] for col in inspector.get_columns(table)}
    if column in existing:
        return False
    with engine.begin() as conn:
        conn.execute(text(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {ddl}'))
    return True


def _backfill_expanded_deduplication_keys(engine: Engine) -> None:
    """Backfill deterministic keys and enforce cross-request idempotency."""
    tables = set(inspect(engine).get_table_names())
    with engine.begin() as conn:
        if "notification_dispatches" in tables:
            duplicate_ids = list(
                conn.execute(
                    text(
                        "SELECT id FROM notification_dispatches WHERE id NOT IN ("
                        "SELECT MIN(id) FROM notification_dispatches "
                        "GROUP BY deduplication_key)"
                    )
                ).scalars()
            )
            for dispatch_id in duplicate_ids:
                conn.execute(
                    text("DELETE FROM notification_dispatches WHERE id=:id"),
                    {"id": dispatch_id},
                )
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_notification_dispatch_dedup "
                    "ON notification_dispatches (deduplication_key)"
                )
            )

        if "media_assets" in tables and "deduplication_key" in {
            column["name"] for column in inspect(engine).get_columns("media_assets")
        }:
            rows = conn.execute(
                text(
                    "SELECT id, owner_id, checksum_sha256, status, moderation_status, "
                    "created_at FROM media_assets "
                    "WHERE checksum_sha256 IS NOT NULL ORDER BY created_at, id"
                )
            ).mappings().all()
            grouped: dict[str, list[object]] = {}
            for row in rows:
                key = f"{row['owner_id']}:{row['checksum_sha256']}"
                grouped.setdefault(key, []).append(row)
            for key, duplicates in grouped.items():
                # Preserve a usable asset when an older processing attempt
                # failed.  Timestamp alone is not a safe canonical choice.
                canonical_row = min(
                    duplicates,
                    key=lambda row: (
                        0 if row["status"] == "READY" else 1,
                        0 if row["moderation_status"] == "approved" else 1,
                        row["created_at"],
                        str(row["id"]),
                    ),
                )
                canonical_id = str(canonical_row["id"])
                conn.execute(
                    text("UPDATE media_assets SET deduplication_key=:key WHERE id=:id"),
                    {"key": key, "id": canonical_row["id"]},
                )
                for row in duplicates:
                    if str(row["id"]) == canonical_id:
                        continue
                    conn.execute(
                        text(
                            "UPDATE media_assets SET status='DELETED', deduplication_key=NULL, "
                            "processing_error=:reason WHERE id=:id"
                        ),
                        {"reason": f"DUPLICATE_OF:{canonical_id}", "id": row["id"]},
                    )
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_media_asset_deduplication_key "
                    "ON media_assets (deduplication_key)"
                )
            )

        if "share_attribution_events" in tables and "deduplication_key" in {
            column["name"]
            for column in inspect(engine).get_columns("share_attribution_events")
        }:
            rows = list(
                conn.execute(
                    text(
                        "SELECT id, share_id, event_type, user_id, anonymous_session_id, "
                        "business_id, created_at FROM share_attribution_events "
                        "ORDER BY created_at, id"
                    )
                ).mappings()
            )
            canonical: dict[str, str] = {}
            for row in rows:
                identity = row["user_id"] or row["anonymous_session_id"] or "guest"
                key = hashlib.sha256(
                    "|".join(
                        (
                            str(row["share_id"]),
                            str(row["event_type"]),
                            str(identity),
                            str(row["business_id"] or ""),
                        )
                    ).encode("utf-8")
                ).hexdigest()
                if key in canonical:
                    conn.execute(
                        text("DELETE FROM share_attribution_events WHERE id=:id"),
                        {"id": row["id"]},
                    )
                else:
                    canonical[key] = str(row["id"])
                    conn.execute(
                        text(
                            "UPDATE share_attribution_events SET deduplication_key=:key "
                            "WHERE id=:id"
                        ),
                        {"key": key, "id": row["id"]},
                    )
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_share_attribution_event_dedup "
                    "ON share_attribution_events (deduplication_key)"
                )
            )
            if engine.dialect.name == "postgresql":
                conn.execute(
                    text(
                        "ALTER TABLE share_attribution_events "
                        "ALTER COLUMN deduplication_key SET NOT NULL"
                    )
                )
            elif engine.dialect.name == "sqlite":
                conn.execute(
                    text(
                        "CREATE TRIGGER IF NOT EXISTS trg_share_attribution_dedup_not_null_insert "
                        "BEFORE INSERT ON share_attribution_events "
                        "WHEN NEW.deduplication_key IS NULL BEGIN "
                        "SELECT RAISE(ABORT, 'share attribution deduplication key is required'); END"
                    )
                )
                conn.execute(
                    text(
                        "CREATE TRIGGER IF NOT EXISTS trg_share_attribution_dedup_not_null_update "
                        "BEFORE UPDATE OF deduplication_key ON share_attribution_events "
                        "WHEN NEW.deduplication_key IS NULL BEGIN "
                        "SELECT RAISE(ABORT, 'share attribution deduplication key is required'); END"
                    )
                )
            if "share_records" in tables:
                # Historical duplicate payment events may have already
                # inflated this denormalized value. Recompute it from the
                # canonical event rows after cleanup.
                conn.execute(
                    text(
                        "UPDATE share_records SET conversion_count=("
                        "SELECT COUNT(*) FROM share_attribution_events "
                        "WHERE share_attribution_events.share_id=share_records.id "
                        "AND share_attribution_events.event_type='payment'"
                        ")"
                    )
                )


def _postgres_migrate_expanded_requirements(engine: Engine) -> None:
    """Apply additive expanded-requirement schema changes in production.

    ``Base.metadata.create_all`` creates new tables but never adds columns to an
    existing table. These explicit, idempotent changes prevent a deployed API
    from starting with models that do not match the production schema.
    """
    additions = {
        "messages": (
            ("message_type", "VARCHAR(30) DEFAULT 'text' NOT NULL"),
            ("structured_payload_json", "TEXT DEFAULT '{}' NOT NULL"),
            ("official_platform_message", "BOOLEAN DEFAULT FALSE NOT NULL"),
        ),
        "system_notifications": (
            ("notification_category", "VARCHAR(40)"),
            ("user_role_context", "VARCHAR(20)"),
            ("notification_type", "VARCHAR(50)"),
            ("business_type", "VARCHAR(30)"),
            ("business_id", "VARCHAR(50)"),
            ("deep_link", "VARCHAR(500)"),
            ("push_status", "VARCHAR(20) DEFAULT 'pending' NOT NULL"),
            ("read_at", "TIMESTAMP WITH TIME ZONE"),
        ),
        "orders": (
            ("private_offer_id", "VARCHAR(36)"),
        ),
        "listings": (
            ("videos_json", "TEXT DEFAULT '[]' NOT NULL"),
            ("thumbnail_url", "VARCHAR(1000)"),
        ),
        "notification_dispatches": (
            ("payload_json", "TEXT DEFAULT '{}' NOT NULL"),
            ("attempt_count", "INTEGER DEFAULT 0 NOT NULL"),
            ("last_attempt_at", "TIMESTAMP WITH TIME ZONE"),
        ),
        "media_assets": (
            ("security_scan_status", "VARCHAR(20) DEFAULT 'pending' NOT NULL"),
            ("source_storage_key", "VARCHAR(500)"),
            ("source_url", "VARCHAR(1000)"),
            ("automatic_retry_count", "INTEGER DEFAULT 0 NOT NULL"),
            ("processing_lease_token", "VARCHAR(64)"),
            ("processing_lease_until", "TIMESTAMP WITH TIME ZONE"),
            ("deduplication_key", "VARCHAR(128)"),
        ),
        "share_attribution_events": (
            ("deduplication_key", "VARCHAR(64)"),
        ),
    }
    for table, columns in additions.items():
        for column, ddl in columns:
            _postgres_add_column_if_missing(engine, table, column, ddl)
    tables = set(inspect(engine).get_table_names())
    with engine.begin() as conn:
        if "orders" in tables:
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_orders_private_offer_id "
                    "ON orders (private_offer_id) WHERE private_offer_id IS NOT NULL"
                )
            )
    _backfill_expanded_deduplication_keys(engine)


def _migrate_legacy_au_phones_to_e164(engine: Engine) -> None:
    """Canonicalize legacy Australian phone identities without inventing merges.

    Every phone identity is globally stored in E.164. If a legacy and canonical
    row already coexist, the legacy row is deliberately left untouched so the
    authorized account-merge flow can resolve ownership instead of silently
    combining two accounts.
    """
    tables = set(inspect(engine).get_table_names())
    if "users" not in tables:
        return

    def canonical(value: object) -> str | None:
        raw = str(value or "").strip()
        if len(raw) == 10 and raw.startswith("0") and raw.isdigit():
            return f"+61{raw[1:]}"
        return None

    with engine.begin() as conn:
        users = conn.execute(text("SELECT id, phone FROM users WHERE phone IS NOT NULL")).fetchall()
        occupied = {str(phone) for _, phone in users if phone}
        migrated_users: dict[str, str] = {}
        for user_id, phone in users:
            normalized = canonical(phone)
            if not normalized or normalized in occupied:
                continue
            conn.execute(
                text("UPDATE users SET phone = :phone WHERE id = :user_id"),
                {"phone": normalized, "user_id": user_id},
            )
            occupied.discard(str(phone))
            occupied.add(normalized)
            migrated_users[str(phone)] = normalized

        if "phone_otps" in tables:
            otp_rows = conn.execute(
                text("SELECT id, phone, purpose FROM phone_otps")
            ).fetchall()
            existing_otp = {(str(phone), str(purpose)) for _, phone, purpose in otp_rows}
            for otp_id, phone, purpose in otp_rows:
                normalized = canonical(phone)
                if not normalized:
                    continue
                if (normalized, str(purpose)) in existing_otp:
                    conn.execute(
                        text("DELETE FROM phone_otps WHERE id = :otp_id"),
                        {"otp_id": otp_id},
                    )
                else:
                    conn.execute(
                        text("UPDATE phone_otps SET phone = :phone WHERE id = :otp_id"),
                        {"phone": normalized, "otp_id": otp_id},
                    )
                    existing_otp.add((normalized, str(purpose)))

        if "auth_identities" in tables:
            identity_rows = conn.execute(
                text(
                    "SELECT id, user_id, provider_subject FROM auth_identities "
                    "WHERE provider = 'phone'"
                )
            ).fetchall()
            occupied_subjects = {str(subject) for _, _, subject in identity_rows}
            for identity_id, user_id, subject in identity_rows:
                normalized = canonical(subject)
                if not normalized:
                    continue
                if normalized in occupied_subjects:
                    continue
                conn.execute(
                    text(
                        "UPDATE auth_identities SET provider_subject = :subject "
                        "WHERE id = :identity_id"
                    ),
                    {"subject": normalized, "identity_id": identity_id},
                )
                occupied_subjects.discard(str(subject))
                occupied_subjects.add(normalized)

        if "login_audit_logs" in tables:
            for old_phone, normalized in migrated_users.items():
                conn.execute(
                    text(
                        "UPDATE login_audit_logs SET subject_hint = :normalized "
                        "WHERE provider = 'phone' AND subject_hint = :old_phone"
                    ),
                    {"normalized": normalized, "old_phone": old_phone},
                )


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
    dialect = engine.dialect.name
    if dialect == "postgresql":
        _postgres_migrate_expanded_requirements(engine)
        _migrate_legacy_au_phones_to_e164(engine)
        return
    if dialect != "sqlite":
        return

    _sqlite_add_column_if_missing(engine, "users", "email", "email VARCHAR(255)")
    _sqlite_add_column_if_missing(engine, "system_notifications", "category", "category VARCHAR(20) DEFAULT 'system'")
    _sqlite_add_column_if_missing(
        engine,
        "system_notifications",
        "notification_category",
        "notification_category VARCHAR(40)",
    )
    _sqlite_add_column_if_missing(engine, "system_notifications", "title_zh", "title_zh VARCHAR(200)")
    _sqlite_add_column_if_missing(engine, "system_notifications", "body_zh", "body_zh TEXT")
    _sqlite_add_column_if_missing(engine, "system_notifications", "action_type", "action_type VARCHAR(30)")
    _sqlite_add_column_if_missing(engine, "system_notifications", "action_ref", "action_ref VARCHAR(50)")
    _sqlite_add_column_if_missing(
        engine, "system_notifications", "user_role_context", "user_role_context VARCHAR(20)"
    )
    _sqlite_add_column_if_missing(
        engine, "system_notifications", "notification_type", "notification_type VARCHAR(50)"
    )
    _sqlite_add_column_if_missing(
        engine, "system_notifications", "business_type", "business_type VARCHAR(30)"
    )
    _sqlite_add_column_if_missing(
        engine, "system_notifications", "business_id", "business_id VARCHAR(50)"
    )
    _sqlite_add_column_if_missing(
        engine, "system_notifications", "deep_link", "deep_link VARCHAR(500)"
    )
    _sqlite_add_column_if_missing(
        engine, "system_notifications", "push_status", "push_status VARCHAR(20) DEFAULT 'pending'"
    )
    _sqlite_add_column_if_missing(engine, "system_notifications", "read_at", "read_at DATETIME")
    _sqlite_add_column_if_missing(engine, "messages", "message_type", "message_type VARCHAR(30) DEFAULT 'text'")
    _sqlite_add_column_if_missing(
        engine, "messages", "structured_payload_json", "structured_payload_json TEXT DEFAULT '{}'"
    )
    _sqlite_add_column_if_missing(
        engine, "messages", "official_platform_message", "official_platform_message BOOLEAN DEFAULT 0"
    )
    _sqlite_add_column_if_missing(engine, "listings", "bundle_meta_json", "bundle_meta_json TEXT DEFAULT '{}'")
    _sqlite_add_column_if_missing(engine, "listings", "videos_json", "videos_json TEXT DEFAULT '[]'")
    _sqlite_add_column_if_missing(engine, "listings", "thumbnail_url", "thumbnail_url VARCHAR(1000)")
    _sqlite_add_column_if_missing(
        engine,
        "media_assets",
        "security_scan_status",
        "security_scan_status VARCHAR(20) DEFAULT 'pending'",
    )
    _sqlite_add_column_if_missing(
        engine,
        "media_assets",
        "source_storage_key",
        "source_storage_key VARCHAR(500)",
    )
    _sqlite_add_column_if_missing(
        engine,
        "media_assets",
        "source_url",
        "source_url VARCHAR(1000)",
    )
    _sqlite_add_column_if_missing(
        engine,
        "media_assets",
        "automatic_retry_count",
        "automatic_retry_count INTEGER DEFAULT 0",
    )
    _sqlite_add_column_if_missing(
        engine,
        "media_assets",
        "processing_lease_token",
        "processing_lease_token VARCHAR(64)",
    )
    _sqlite_add_column_if_missing(
        engine,
        "media_assets",
        "processing_lease_until",
        "processing_lease_until DATETIME",
    )
    _sqlite_add_column_if_missing(
        engine,
        "media_assets",
        "deduplication_key",
        "deduplication_key VARCHAR(128)",
    )
    _sqlite_add_column_if_missing(
        engine,
        "share_attribution_events",
        "deduplication_key",
        "deduplication_key VARCHAR(64)",
    )
    _backfill_expanded_deduplication_keys(engine)

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
    _sqlite_add_column_if_missing(engine, "orders", "private_offer_id", "private_offer_id VARCHAR(36)")
    _sqlite_add_column_if_missing(engine, "orders", "coupon_id", "coupon_id VARCHAR(36)")
    _sqlite_add_column_if_missing(engine, "orders", "discount_amount", "discount_amount FLOAT DEFAULT 0")
    _sqlite_add_column_if_missing(engine, "listings", "meet_in_public", "meet_in_public BOOLEAN DEFAULT 1")
    _sqlite_add_column_if_missing(engine, "user_settings", "remind_pay", "remind_pay BOOLEAN DEFAULT 1")
    _sqlite_add_column_if_missing(engine, "user_settings", "remind_ship", "remind_ship BOOLEAN DEFAULT 1")
    _sqlite_add_column_if_missing(engine, "user_settings", "remind_receive", "remind_receive BOOLEAN DEFAULT 1")
    _sqlite_add_column_if_missing(engine, "user_settings", "remind_dispute", "remind_dispute BOOLEAN DEFAULT 1")
    _migrate_legacy_au_phones_to_e164(engine)

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
    if "notification_dispatches" in set(inspect(engine).get_table_names()):
        _sqlite_add_column_if_missing(
            engine,
            "notification_dispatches",
            "payload_json",
            "payload_json TEXT DEFAULT '{}'",
        )
        _sqlite_add_column_if_missing(
            engine,
            "notification_dispatches",
            "attempt_count",
            "attempt_count INTEGER DEFAULT 0",
        )
        _sqlite_add_column_if_missing(
            engine,
            "notification_dispatches",
            "last_attempt_at",
            "last_attempt_at DATETIME",
        )
        with engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_orders_private_offer_id "
                    "ON orders (private_offer_id) WHERE private_offer_id IS NOT NULL"
                )
            )


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
        ("refund_status", "refund_status VARCHAR(30)"),
        ("refund_reference", "refund_reference VARCHAR(100)"),
        ("refund_failure_code", "refund_failure_code VARCHAR(50)"),
        ("refund_failure_reason", "refund_failure_reason TEXT"),
        ("refunded_at", "refunded_at DATETIME"),
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
