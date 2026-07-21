from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parent.parent / ".env",
        extra="ignore",
    )

    database_url: str = "sqlite:///./heishi.db"
    app_environment: str = "development"
    jwt_secret: str = "dev-secret-change-in-production"
    jwt_access_expire_seconds: int = 3600
    jwt_refresh_expire_days: int = 30
    base_url: str = "http://127.0.0.1:8000"
    cors_origins: str = "*"
    upload_dir: str = "uploads"
    storage_backend: str = "local"
    supabase_storage_bucket: str = ""
    supabase_storage_path_prefix: str = "uploads"
    retain_original_media: bool = True
    # Media threat scanning. ``signature`` performs the built-in file-signature
    # validation used for local development. Production should use ``clamav``
    # and point these settings at a private ClamAV daemon; uploads fail closed
    # when that scanner is unavailable.
    media_security_scan_mode: str = "signature"
    clamav_host: str = "127.0.0.1"
    clamav_port: int = 3310
    clamav_timeout_seconds: float = 15.0
    escrow_fee: float = 0.0
    aud_to_cny_display_rate: float = 4.75
    pending_pay_expire_minutes: int = 30
    pending_pay_reminder_minutes: int = 15
    pending_pay_deadline_reminder_minutes_before: int = 5
    background_jobs_interval_seconds: int = 60
    # Automatically suspend a public share token once it reaches an implausibly
    # high number of resolutions. This limits replay/scraping abuse while keeping
    # the threshold configurable for campaigns with legitimate high traffic.
    share_max_access_count: int = 10000
    # HTTPS origin used for verified product links. Keep blank locally; set to
    # the production web origin (for example https://market.example.com).
    public_app_url: str = ""
    android_app_package: str = "com.heishi.mvp"
    android_app_sha256_fingerprints: str = ""
    apple_team_id: str = ""
    apple_bundle_id: str = "com.heishi.mvp"
    # App Store destination used by the share landing page. The install button
    # first copies the structured share command so the privacy-compliant
    # clipboard entry flow can restore the product after installation.
    ios_app_store_url: str = ""
    chat_messages: bool = True
    remind_pay: bool = True
    remind_ship: bool = True
    show_wechat_badge: bool = False
    admin_seed_phone: str = "0499999001"
    admin_seed_password: str = "Admin123!"
    # Legacy phone-OTP dev mode: when true, register/login send-code responses include the
    # OTP as `devCode` (no real SMS provider wired yet). Set false in production once Twilio
    # is configured so codes are delivered by SMS only.
    expose_dev_otp: bool = True
    # Local verification mode for development QA. When true, Twilio is bypassed even if
    # Twilio credentials are present; the API stores a temporary local OTP and can expose
    # it as `devCode` when EXPOSE_DEV_OTP is enabled.
    sms_dev_otp: bool = False
    # Payments. When a Stripe secret key is present the real Stripe flow (SetupIntent /
    # PaymentSheet / Connect / PaymentIntent) activates; otherwise the API simulates
    # payments so local/offline dev keeps working. `payments_simulated` force-simulates
    # even if a key is set (handy for staging). Effective real mode = stripe_enabled.
    payments_simulated: bool = True
    stripe_secret_key: str = ""
    stripe_publishable_key: str = ""
    stripe_webhook_secret: str = ""
    default_charge_currency: str = "aud"
    # Legal country for newly created Stripe Connect Express seller accounts.
    # This is immutable after onboarding starts, so set it explicitly instead of
    # allowing Stripe to inherit the platform account's country.
    stripe_connect_country: str = "AU"
    # Stripe Connect onboarding return/refresh deep links (app scheme). Overridable per env.
    connect_return_url: str = "heishi://payout/connect/return"
    connect_refresh_url: str = "heishi://payout/connect/refresh"
    paypal_client_id: str = ""
    paypal_client_secret: str = ""
    # PayPal Commerce Platform attribution code shown on the Platform app page.
    paypal_partner_attribution_id: str = ""
    # Optional platform PayPal Merchant ID. Seller onboarding can complete without it;
    # when configured it enables PayPal's merchant-integration capability verification.
    paypal_partner_merchant_id: str = ""
    # PayPal assigns this after registering the public webhook URL. Incoming
    # webhook events are rejected unless their signature validates against it.
    paypal_webhook_id: str = ""
    # PayPal environment is independent from Stripe simulation. This allows Stripe
    # test-mode API calls and PayPal sandbox API calls in the same escrow test stack.
    paypal_sandbox: bool = True
    alipay_app_id: str = ""
    alipay_private_key: str = ""
    alipay_public_key: str = ""
    # Registered Alipay OAuth callback. Production should use a verified HTTPS
    # app-link URL; local native builds may use the application scheme.
    alipay_oauth_redirect_url: str = "heymarket://auth/alipay"
    # WeChat Open Platform login (not WeChat Pay). The mobile app obtains an
    # authorization code, then the backend exchanges it for openid/unionid.
    wechat_open_app_id: str = ""
    wechat_open_app_secret: str = ""
    wechat_pay_app_id: str = ""
    wechat_pay_mch_id: str = ""
    wechat_pay_api_v3_key: str = ""
    wechat_pay_serial_no: str = ""
    wechat_pay_private_key: str = ""

    @property
    def stripe_enabled(self) -> bool:
        """Real Stripe integration (SetupIntent/PaymentSheet, Connect, PaymentIntent) is
        live when a secret key is present and simulation is off — the same switch the
        checkout adapters use (`payments_simulated`). To go live the client provides the
        keys and sets `payments_simulated=false`; otherwise the API simulates so local
        dev keeps working."""
        return bool(self.stripe_secret_key.strip()) and not self.payments_simulated

    @property
    def paypal_payout_enabled(self) -> bool:
        return bool(self.paypal_client_id.strip() and self.paypal_client_secret.strip())

    @property
    def alipay_payout_enabled(self) -> bool:
        return bool(
            self.alipay_app_id.strip()
            and self.alipay_private_key.strip()
            and self.alipay_public_key.strip()
        )

    @property
    def wechat_payout_enabled(self) -> bool:
        return bool(
            self.wechat_pay_app_id.strip()
            and self.wechat_pay_mch_id.strip()
            and self.wechat_pay_api_v3_key.strip()
            and self.wechat_pay_serial_no.strip()
            and self.wechat_pay_private_key.strip()
        )

    # Supabase Auth (Path A — phone OTP). When jwt_secret is set, API accepts Supabase JWTs.
    supabase_url: str = ""
    supabase_jwt_secret: str = ""
    supabase_service_role_key: str = ""
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_verify_service_sid: str = ""
    # Optional transactional-notification sender. Configure either a Messaging
    # Service SID or a Twilio phone number; Verify remains dedicated to OTP.
    twilio_messaging_service_sid: str = ""
    twilio_from_phone: str = ""
    google_oauth_client_id: str = ""
    google_dev_auth_fallback: bool = False

    @property
    def supabase_auth_enabled(self) -> bool:
        return bool(self.supabase_jwt_secret.strip())

    @property
    def twilio_verify_enabled(self) -> bool:
        return bool(
            self.twilio_account_sid.strip()
            and self.twilio_auth_token.strip()
            and self.twilio_verify_service_sid.strip()
        )

    @property
    def twilio_verify_partially_configured(self) -> bool:
        values = [
            self.twilio_account_sid.strip(),
            self.twilio_auth_token.strip(),
            self.twilio_verify_service_sid.strip(),
        ]
        return any(values) and not self.twilio_verify_enabled

    @property
    def google_oauth_client_ids(self) -> list[str]:
        return [value.strip() for value in self.google_oauth_client_id.split(",") if value.strip()]

    @property
    def cors_origin_list(self) -> list[str]:
        if self.cors_origins.strip() == "*":
            return ["*"]
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()


def validate_runtime_configuration() -> None:
    """Fail fast when production would silently disable mandatory safeguards."""
    if settings.app_environment.strip().lower() != "production":
        return
    errors: list[str] = []
    if settings.jwt_secret == "dev-secret-change-in-production" or len(settings.jwt_secret) < 32:
        errors.append("JWT_SECRET must be a production secret of at least 32 characters")
    if settings.media_security_scan_mode.strip().lower() != "clamav":
        errors.append("MEDIA_SECURITY_SCAN_MODE=clamav is required in production")
    if settings.storage_backend.strip().lower() != "supabase":
        errors.append("STORAGE_BACKEND=supabase is required for production media delivery")
    if not settings.public_app_url.strip().startswith("https://"):
        errors.append("PUBLIC_APP_URL must be a verified HTTPS origin")
    if not settings.android_app_sha256_fingerprints.strip():
        errors.append(
            "ANDROID_APP_SHA256_FINGERPRINTS is required for verified Android app links"
        )
    if not settings.apple_team_id.strip():
        errors.append("APPLE_TEAM_ID is required for verified iOS universal links")
    from app.video_processing import video_processor_available

    if not video_processor_available():
        errors.append("FFmpeg and FFprobe are required for production video processing")
    if errors:
        raise RuntimeError("Invalid production configuration: " + "; ".join(errors))
