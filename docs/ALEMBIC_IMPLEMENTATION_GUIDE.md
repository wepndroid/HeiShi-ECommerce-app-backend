# Alembic Implementation Guide

This guide explains how to add Alembic migrations to the HeyMarket backend so
Supabase/Postgres schema changes are controlled, repeatable, and safe after
production launch.

## Why Alembic Is Needed

The backend currently creates tables at startup with:

```python
Base.metadata.create_all(bind=engine)
```

That is acceptable for a fresh empty database, but it is not a production
migration system.

`create_all()` can create missing tables, but it does not reliably update
existing production tables when SQLAlchemy models change. It will not safely
rename columns, change column types, backfill data, drop constraints, or preserve
a reviewed history of schema changes.

The existing `app/migrations.py` file is also SQLite-only. It exits immediately
for Postgres/Supabase:

```python
if not str(engine.url).startswith("sqlite"):
    return
```

So once Supabase is used in production, model changes should be applied through
Alembic migrations.

## Target Workflow

After Alembic is implemented, the production schema workflow should be:

1. Update SQLAlchemy models in `app/models.py`.
2. Generate an Alembic revision.
3. Review and edit the migration file.
4. Test the migration locally or against a staging database.
5. Apply migrations to Supabase.
6. Deploy the backend code that expects the new schema.

## Required Package

Add Alembic to `requirements.txt`:

```text
alembic==1.14.0
```

Any recent Alembic version compatible with SQLAlchemy 2.x is acceptable. Pinning
the version keeps Railway builds reproducible.

## Initial Setup Commands

Run these from the `Backend/` folder:

```powershell
.\.venv\Scripts\python.exe -m pip install alembic==1.14.0
.\.venv\Scripts\python.exe -m alembic init alembic
```

This creates:

```text
alembic.ini
alembic/
  env.py
  script.py.mako
  versions/
```

Commit these files.

## Configure Alembic To Use App Settings

Edit `alembic/env.py` so Alembic uses the backend's existing SQLAlchemy metadata
and `DATABASE_URL`.

The important imports are:

```python
from app.config import settings
from app.database import Base
import app.models
```

`import app.models` is needed because the model classes must be imported before
Alembic can see their tables through `Base.metadata`.

Set:

```python
target_metadata = Base.metadata
```

Then make Alembic read the URL from settings:

```python
config.set_main_option("sqlalchemy.url", settings.database_url)
```

Do not hard-code production credentials in `alembic.ini`.

## Recommended `alembic/env.py` Shape

Use the default Alembic file, but make sure the key parts look like this:

```python
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import settings
from app.database import Base
import app.models  # noqa: F401

config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
```

`compare_type=True` helps Alembic notice type changes during autogeneration.

## Baseline Migration

For a brand-new Supabase database, create the initial migration from current
models:

```powershell
.\.venv\Scripts\python.exe -m alembic revision --autogenerate -m "initial schema"
```

Review the generated file in:

```text
alembic/versions/
```

Then apply it:

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
```

This creates the schema and writes the current revision into Alembic's
`alembic_version` table.

## If Tables Already Exist In Supabase

If the backend has already created tables with `Base.metadata.create_all()`, do
not blindly run an initial migration that tries to create the same tables again.

Use one of these approaches:

### Option A: Fresh Database Reset

Use this only before real production data exists.

1. Drop/recreate the Supabase database tables.
2. Run `alembic upgrade head`.
3. Start the backend.

This is the cleanest approach before launch.

### Option B: Stamp Existing Schema

Use this when the existing Supabase schema already matches the current models.

```powershell
.\.venv\Scripts\python.exe -m alembic stamp head
```

This records the current migration revision without running schema changes.

Only use `stamp head` after verifying the actual database schema matches the
models.

## Remove Or Reduce `create_all()`

After Alembic is in place, the backend should stop relying on:

```python
Base.metadata.create_all(bind=engine)
```

Recommended production behavior:

- Local development may still use `create_all()` only if explicitly enabled.
- Production should require `alembic upgrade head` before app startup.

A practical transition is to add an env variable:

```env
AUTO_CREATE_TABLES=false
```

Then in `app/config.py`:

```python
auto_create_tables: bool = False
```

And in `app/main.py`:

```python
if settings.auto_create_tables:
    Base.metadata.create_all(bind=engine)
```

For production, keep:

```env
AUTO_CREATE_TABLES=false
```

## Railway Deployment Options

There are two safe ways to run migrations with Railway.

### Option A: Manual Migration Before Deploy

Run migrations from a trusted local machine using production `DATABASE_URL`, then
deploy the backend.

Example:

```powershell
$env:DATABASE_URL="postgresql+psycopg2://..."
.\.venv\Scripts\python.exe -m alembic upgrade head
```

This gives the most control because you see migration output before deploying.

### Option B: Railway Start Command Runs Migrations

Change the Railway start command to run migrations before Uvicorn:

```text
python -m alembic upgrade head && python -m uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

This is convenient, but if a migration fails the app will not start. That is good
for safety, but the deployment logs must be checked carefully.

If using `railway.toml`, update:

```toml
[deploy]
startCommand = "python -m alembic upgrade head && python -m uvicorn app.main:app --host 0.0.0.0 --port $PORT"
```

## Generating Future Migrations

After changing models:

```powershell
.\.venv\Scripts\python.exe -m alembic revision --autogenerate -m "describe change"
```

Always review the generated migration before applying it. Alembic autogeneration
is helpful, but it cannot understand every data migration safely.

Apply locally:

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
```

Rollback locally if needed:

```powershell
.\.venv\Scripts\python.exe -m alembic downgrade -1
```

Do not run downgrade on production unless the downgrade path has been reviewed
and the data-loss risk is understood.

## Data Migrations

Some schema changes require data transformations.

Examples:

- Splitting `full_name` into `first_name` and `last_name`
- Backfilling a new non-null column
- Converting string status values
- Moving JSON fields into relational columns

For these, edit the Alembic revision manually:

```python
from alembic import op
import sqlalchemy as sa


def upgrade() -> None:
    op.add_column("users", sa.Column("display_name", sa.String(length=100), nullable=True))
    op.execute("update users set display_name = nickname where display_name is null")


def downgrade() -> None:
    op.drop_column("users", "display_name")
```

Avoid making a column `nullable=False` until existing rows have valid values.

## Supabase SQL Editor

Supabase SQL Editor is useful for:

- Inspecting schema
- Running reviewed migration SQL manually
- Verifying data
- Emergency fixes

It should not be the normal place for untracked manual table editing.

Helpful verification query:

```sql
select table_name
from information_schema.tables
where table_schema = 'public'
order by table_name;
```

Check current Alembic revision:

```sql
select * from alembic_version;
```

## Production Checklist

Before first Alembic-backed production deploy:

1. Add `alembic` to `requirements.txt`.
2. Run `alembic init alembic`.
3. Configure `alembic/env.py` to use `settings.database_url` and `Base.metadata`.
4. Generate and review the initial migration.
5. Decide whether Supabase is fresh or needs `stamp head`.
6. Apply migration to Supabase.
7. Set `AUTO_CREATE_TABLES=false` if that guard is implemented.
8. Deploy backend.
9. Check `/health`.
10. Check Supabase `alembic_version`.

## Important Rule

After production data exists, every database schema change should have a tracked
Alembic revision. This keeps Railway, Supabase, local development, and future
debugging all speaking the same language.
