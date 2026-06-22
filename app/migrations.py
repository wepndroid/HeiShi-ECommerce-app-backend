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