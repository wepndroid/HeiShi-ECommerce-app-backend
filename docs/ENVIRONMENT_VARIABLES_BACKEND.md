# Backend Environment Variables

This document explains where to obtain each backend environment variable for the
Railway production deployment.

Do not commit real production secrets. Store real values in Railway service
variables and, if needed for local reference, in `production.env`.

## Official References

- Railway variables: https://docs.railway.com/variables
- Railway public networking: https://docs.railway.com/networking/public-networking
- Supabase database connections: https://supabase.com/docs/guides/database/connecting-to-postgres
- Supabase Storage uploads: https://supabase.com/docs/guides/storage/uploads/standard-uploads

Railway variables are added from the backend service's **Variables** tab. Railway
also supports pasting `.env` contents into the **RAW Editor**.

## Known Production Values

These are values we already know for the current production setup.

```env
DATABASE_URL=postgresql+psycopg2://postgres.bmofailtywcpjenfwqib:[YOUR-PASSWORD]@aws-1-ap-southeast-2.pooler.supabase.com:5432/postgres?sslmode=require
STORAGE_BACKEND=supabase
SUPABASE_STORAGE_BUCKET=listing-images
SUPABASE_STORAGE_PATH_PREFIX=uploads
DEFAULT_CHARGE_CURRENCY=aud
CONNECT_RETURN_URL=heishi://payout/connect/return
CONNECT_REFRESH_URL=heishi://payout/connect/refresh
ESCROW_FEE=0.0
AUD_TO_CNY_DISPLAY_RATE=4.75
PENDING_PAY_EXPIRE_MINUTES=30
CHAT_MESSAGES=true
REMIND_PAY=true
REMIND_SHIP=true
SHOW_WECHAT_BADGE=false
EXPOSE_DEV_OTP=false
```

Replace `[YOUR-PASSWORD]` in `DATABASE_URL` with the actual Supabase database
password. Do not include square brackets.

`BASE_URL` is not known until Railway generates the backend public domain.

## Required Core Variables

### `DATABASE_URL`

Purpose: SQLAlchemy database connection string.

Where to get it:

1. Open Supabase.
2. Open the production project.
3. Click **Connect**.
4. Choose **Session pooler**.
5. Copy the URI.

For Railway, use the Session pooler on port `5432`. Supabase documents Session
pooler as the IPv4-compatible option for persistent backends on IPv4-only
networks.

Backend format:

```env
DATABASE_URL=postgresql+psycopg2://postgres.bmofailtywcpjenfwqib:YOUR_REAL_PASSWORD@aws-1-ap-southeast-2.pooler.supabase.com:5432/postgres?sslmode=require
```

### `JWT_SECRET`

Purpose: signs backend access and refresh tokens.

Where to get it: generate a long random string and store it only in Railway.

Recommended: at least 32 random characters.

### `BASE_URL`

Purpose: public backend origin used for callback URLs and media URL generation.

Where to get it:

1. Open Railway.
2. Open the backend service.
3. Go to **Settings**.
4. Go to **Networking** -> **Public Networking**.
5. Click **Generate Domain**.

Example:

```env
BASE_URL=https://your-backend-service.railway.app
```

Do not add `/v1`.

### `CORS_ORIGINS`

Purpose: browser/admin web CORS allow-list.

For initial testing:

```env
CORS_ORIGINS=*
```

For production, replace with exact web/admin origins:

```env
CORS_ORIGINS=https://your-admin-domain.com,https://your-web-domain.com
```

### `EXPOSE_DEV_OTP`

Purpose: controls whether OTP codes are returned in API responses.

Production value:

```env
EXPOSE_DEV_OTP=false
```

## Token Expiration

### `JWT_ACCESS_EXPIRE_SECONDS`

Purpose: access token lifetime.

Recommended:

```env
JWT_ACCESS_EXPIRE_SECONDS=3600
```

### `JWT_REFRESH_EXPIRE_DAYS`

Purpose: refresh token lifetime.

Recommended:

```env
JWT_REFRESH_EXPIRE_DAYS=30
```

## Supabase Auth And Storage

### `SUPABASE_URL`

Purpose: Supabase project API URL and Storage base URL.

Where to get it:

1. Open Supabase project.
2. Go to **Project Settings** -> **API**.
3. Copy the Project URL.

Format:

```env
SUPABASE_URL=https://bmofailtywcpjenfwqib.supabase.co
```

### `SUPABASE_JWT_SECRET`

Purpose: allows backend to verify Supabase Auth JWTs.

Where to get it:

1. Open Supabase project.
2. Go to **Project Settings** -> **API**.
3. Copy the JWT Secret.

### `SUPABASE_SERVICE_ROLE_KEY`

Purpose: backend-only key used for Supabase Storage uploads when
`STORAGE_BACKEND=supabase`.

Where to get it:

1. Open Supabase project.
2. Go to **Project Settings** -> **API**.
3. Copy the service role key.

Important: never put this value in frontend/mobile env files.

### `STORAGE_BACKEND`

Purpose: selects upload destination.

Production:

```env
STORAGE_BACKEND=supabase
```

Local-only fallback:

```env
STORAGE_BACKEND=local
```

### `SUPABASE_STORAGE_BUCKET`

Purpose: Supabase Storage bucket used for uploaded listing/profile images.

Recommended production value:

```env
SUPABASE_STORAGE_BUCKET=listing-images
```

Create this bucket in Supabase Storage and make it public so the backend can
return public image URLs to the mobile app.

### `SUPABASE_STORAGE_PATH_PREFIX`

Purpose: object key prefix inside the bucket.

Recommended:

```env
SUPABASE_STORAGE_PATH_PREFIX=uploads
```

### `UPLOAD_DIR`

Purpose: local filesystem upload folder when `STORAGE_BACKEND=local`.

Production with Supabase Storage: not used.

Local default:

```env
UPLOAD_DIR=uploads
```

## Admin Seed

### `ADMIN_SEED_PHONE`

Purpose: phone for the initial seeded admin account.

Production: choose the real admin phone number before first boot.

### `ADMIN_SEED_PASSWORD`

Purpose: password for the initial seeded admin account.

Production: use a strong password before first boot.

## Phone Login

### `TWILIO_ACCOUNT_SID`

Purpose: Twilio account identifier.

Where to get it: Twilio Console.

### `TWILIO_AUTH_TOKEN`

Purpose: Twilio API secret.

Where to get it: Twilio Console.

### `TWILIO_VERIFY_SERVICE_SID`

Purpose: Twilio Verify service used for OTP delivery and verification.

Where to get it: Twilio Console -> Verify -> Services.

If any of these three values are missing, backend Twilio Verify is disabled.

## WeChat Login

### `WECHAT_OPEN_APP_ID`

Purpose: WeChat Open Platform app ID for login/sign-up.

Where to get it: WeChat Open Platform app settings.

### `WECHAT_OPEN_APP_SECRET`

Purpose: WeChat Open Platform app secret used by `/auth/wechat`.

Where to get it: WeChat Open Platform app settings.

## Stripe Payments

### `PAYMENTS_SIMULATED`

Purpose: switches between simulated payments and real provider integrations.

Initial production deployment before Stripe is verified:

```env
PAYMENTS_SIMULATED=true
```

Only after Stripe keys, webhooks, and Connect are configured:

```env
PAYMENTS_SIMULATED=false
```

### `STRIPE_SECRET_KEY`

Purpose: backend Stripe API key.

Where to get it: Stripe Dashboard -> Developers -> API keys.

### `STRIPE_PUBLISHABLE_KEY`

Purpose: publishable Stripe key returned to the app for PaymentSheet.

Where to get it: Stripe Dashboard -> Developers -> API keys.

The frontend/mobile app must use the matching publishable key.

### `STRIPE_WEBHOOK_SECRET`

Purpose: verifies Stripe webhook signatures.

Where to get it:

1. Open Stripe Dashboard.
2. Go to **Developers** -> **Webhooks**.
3. Create endpoint for:

```text
https://YOUR_BASE_URL/v1/payments/webhooks/stripe
```

4. Copy the signing secret beginning with `whsec_`.

### `DEFAULT_CHARGE_CURRENCY`

Purpose: buyer charge currency.

Current production value:

```env
DEFAULT_CHARGE_CURRENCY=aud
```

### `CONNECT_RETURN_URL`

Purpose: app deep link after successful Stripe Connect onboarding.

Current value:

```env
CONNECT_RETURN_URL=heishi://payout/connect/return
```

### `CONNECT_REFRESH_URL`

Purpose: app deep link when Stripe Connect onboarding must be refreshed.

Current value:

```env
CONNECT_REFRESH_URL=heishi://payout/connect/refresh
```

## PayPal

### `PAYPAL_CLIENT_ID`

Purpose: PayPal API client ID.

Where to get it: PayPal Developer Dashboard.

### `PAYPAL_CLIENT_SECRET`

Purpose: PayPal API secret.

Where to get it: PayPal Developer Dashboard.

## Alipay Payouts

### `ALIPAY_APP_ID`

Purpose: Alipay app identifier.

Where to get it: Alipay Open Platform.

### `ALIPAY_PRIVATE_KEY`

Purpose: backend signing private key.

Where to get it: generated during Alipay app/API setup.

### `ALIPAY_PUBLIC_KEY`

Purpose: Alipay public key for API verification.

Where to get it: Alipay Open Platform.

## WeChat Pay Payouts

### `WECHAT_PAY_APP_ID`

Purpose: WeChat Pay app ID.

Where to get it: WeChat Pay merchant/app settings.

### `WECHAT_PAY_MCH_ID`

Purpose: WeChat Pay merchant ID.

Where to get it: WeChat Pay merchant platform.

### `WECHAT_PAY_API_V3_KEY`

Purpose: WeChat Pay API v3 key.

Where to get it: WeChat Pay merchant platform.

### `WECHAT_PAY_SERIAL_NO`

Purpose: merchant certificate serial number.

Where to get it: WeChat Pay merchant certificate settings.

### `WECHAT_PAY_PRIVATE_KEY`

Purpose: merchant private key used to sign WeChat Pay payout requests.

Where to get it: generated during WeChat Pay certificate setup.

## Marketplace Defaults

### `ESCROW_FEE`

Purpose: default escrow fee fallback.

Current value:

```env
ESCROW_FEE=0.0
```

### `AUD_TO_CNY_DISPLAY_RATE`

Purpose: display conversion rate for AUD to CNY.

Current value:

```env
AUD_TO_CNY_DISPLAY_RATE=4.75
```

### `PENDING_PAY_EXPIRE_MINUTES`

Purpose: time before stale pending-payment orders expire.

Current value:

```env
PENDING_PAY_EXPIRE_MINUTES=30
```

## Notification/Profile Defaults

These are default user setting values used by the backend.

```env
CHAT_MESSAGES=true
REMIND_PAY=true
REMIND_SHIP=true
SHOW_WECHAT_BADGE=false
```

## Railway Input Checklist

Paste these keys into Railway's backend service variables, replacing placeholder
values with real secrets:

```env
DATABASE_URL=postgresql+psycopg2://postgres.bmofailtywcpjenfwqib:YOUR_REAL_PASSWORD@aws-1-ap-southeast-2.pooler.supabase.com:5432/postgres?sslmode=require
JWT_SECRET=GENERATE_A_LONG_RANDOM_SECRET
JWT_ACCESS_EXPIRE_SECONDS=3600
JWT_REFRESH_EXPIRE_DAYS=30
BASE_URL=https://YOUR_RAILWAY_DOMAIN
CORS_ORIGINS=*
STORAGE_BACKEND=supabase
SUPABASE_URL=https://bmofailtywcpjenfwqib.supabase.co
SUPABASE_JWT_SECRET=YOUR_SUPABASE_JWT_SECRET
SUPABASE_SERVICE_ROLE_KEY=YOUR_SUPABASE_SERVICE_ROLE_KEY
SUPABASE_STORAGE_BUCKET=listing-images
SUPABASE_STORAGE_PATH_PREFIX=uploads
UPLOAD_DIR=uploads
ESCROW_FEE=0.0
AUD_TO_CNY_DISPLAY_RATE=4.75
PENDING_PAY_EXPIRE_MINUTES=30
CHAT_MESSAGES=true
REMIND_PAY=true
REMIND_SHIP=true
SHOW_WECHAT_BADGE=false
ADMIN_SEED_PHONE=YOUR_ADMIN_PHONE
ADMIN_SEED_PASSWORD=YOUR_STRONG_ADMIN_PASSWORD
EXPOSE_DEV_OTP=false
WECHAT_OPEN_APP_ID=
WECHAT_OPEN_APP_SECRET=
TWILIO_ACCOUNT_SID=
TWILIO_AUTH_TOKEN=
TWILIO_VERIFY_SERVICE_SID=
PAYMENTS_SIMULATED=true
STRIPE_SECRET_KEY=
STRIPE_PUBLISHABLE_KEY=
STRIPE_WEBHOOK_SECRET=
DEFAULT_CHARGE_CURRENCY=aud
CONNECT_RETURN_URL=heishi://payout/connect/return
CONNECT_REFRESH_URL=heishi://payout/connect/refresh
PAYPAL_CLIENT_ID=
PAYPAL_CLIENT_SECRET=
ALIPAY_APP_ID=
ALIPAY_PRIVATE_KEY=
ALIPAY_PUBLIC_KEY=
WECHAT_PAY_APP_ID=
WECHAT_PAY_MCH_ID=
WECHAT_PAY_API_V3_KEY=
WECHAT_PAY_SERIAL_NO=
WECHAT_PAY_PRIVATE_KEY=
```

For the first production smoke test, it is acceptable to leave optional provider
credentials blank and keep `PAYMENTS_SIMULATED=true`. Real Stripe escrow,
refunds, Connect transfers, Twilio SMS, WeChat login, and payout providers only
activate after their credentials are set.
