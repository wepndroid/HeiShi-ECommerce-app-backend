# HeyMarket Backend

Python **FastAPI** service implementing all 63 REST endpoints consumed by the HeyMarket React Native frontend.

**Location:** `HeyMarketApp/Backend` (sibling to `Frontend/`)

## Quick start

```bash
cd Backend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

API base: `http://localhost:8000/v1`  
Interactive docs: `http://localhost:8000/docs`

## Railway deployment

Deploy the `Backend/` service with the included `railway.toml`. It pins the
Railway builder and start command:

```text
python -m uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

Railway provides `PORT` automatically. Deploy this backend Git repository
directly as a single FastAPI service. Attach a Postgres service and set
`DATABASE_URL`, then copy the required production values from `.env.example`. At
minimum for a real deployment, set `JWT_SECRET`, `BASE_URL`, `CORS_ORIGINS`,
`EXPOSE_DEV_OTP=false`, and the auth/payment provider credentials you intend to
enable.

For uploaded photos, attach a Railway Volume if files must survive redeploys and set
`UPLOAD_DIR` to that mounted path, for example `/data/uploads`.

## Frontend connection

In `Frontend/.env`:

```
EXPO_PUBLIC_API_URL=http://localhost:8000/v1
EXPO_PUBLIC_API_MOCK_FALLBACK=false
```

## Phone sign-up and login

The backend supports two phone-OTP modes:

- Supabase Auth phone OTP for the app's Supabase flow
- Twilio Verify for production phone sign-up/login when these backend env vars are set:

```
TWILIO_ACCOUNT_SID=...
TWILIO_AUTH_TOKEN=...
TWILIO_VERIFY_SERVICE_SID=...
```

When Twilio Verify is enabled, `/auth/register/send-code`, `/auth/register`, `/auth/login/send-code`,
and `/auth/login/verify` use Twilio for SMS delivery and code verification. If those env vars
are absent, the backend keeps using the legacy local OTP table as a dev fallback.

If your frontend also has Supabase Auth env vars set for social login, add
`EXPO_PUBLIC_PHONE_AUTH_PROVIDER=backend` in `Frontend/.env` so phone sign-up/login stays on the
backend Twilio path while Supabase remains available for OAuth.

## Demo account

| Field | Value |
|-------|-------|
| Phone | `0400000000` |
| Password | `demo123` |

## API documentation

See `../Documents/backend-api/`:

| Document | Content |
|----------|---------|
| [API-000_Overview.md](../Documents/backend-api/API-000_Overview.md) | Conventions, auth, pagination |
| [API-MASTER_Reference.md](../Documents/backend-api/API-MASTER_Reference.md) | All 63 endpoints |
| API-001 … API-008 | Domain-specific schemas and behavior |

## Architecture

```
app/
├── main.py           # FastAPI app, CORS, error handlers
├── config.py         # Environment settings
├── database.py       # SQLAlchemy engine + session
├── models.py         # ORM models
├── schemas.py        # Pydantic DTOs (mirror frontend types.ts)
├── serializers.py    # Model → DTO mappers
├── auth.py           # JWT + password hashing
├── seed.py           # Demo listings, users, coupons
└── routers/          # Route handlers by domain
```

**Database:** SQLite by default (`heishi.db`). Set `DATABASE_URL` for PostgreSQL in production.

**Uploads:** Stored in `uploads/` and served at `/uploads/{key}`.

## Endpoint coverage

All endpoints from `Frontend/src/api/endpoints/*` are implemented:

- Auth (5), Catalog (6), Listings + Upload (7), Orders (7)
- Favorites, History, Follows, Coupons (10)
- Messaging + Notifications (5)
- Profile, Payments, Payouts, Settings (18)
- Regions, Safety (5)

**Total: 63 endpoints**
