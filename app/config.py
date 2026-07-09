from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parent.parent / ".env",
        extra="ignore",
    )

    database_url: str = "sqlite:///./heishi.db"
    jwt_secret: str = "dev-secret-change-in-production"
    jwt_access_expire_seconds: int = 3600
    jwt_refresh_expire_days: int = 30
    base_url: str = "http://127.0.0.1:8000"
    cors_origins: str = "*"
    upload_dir: str = "uploads"
    escrow_fee: float = 0.0
    aud_to_cny_display_rate: float = 4.75
    pending_pay_expire_minutes: int = 30
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
    # Payments. When a Stripe secret key is present the real Stripe flow (SetupIntent /
    # PaymentSheet / Connect / PaymentIntent) activates; otherwise the API simulates
    # payments so local/offline dev keeps working. `payments_simulated` force-simulates
    # even if a key is set (handy for staging). Effective real mode = stripe_enabled.
    payments_simulated: bool = True
    stripe_secret_key: str = ""
    stripe_publishable_key: str = ""
    stripe_webhook_secret: str = ""
    default_charge_currency: str = "aud"
    # Stripe Connect onboarding return/refresh deep links (app scheme). Overridable per env.
    connect_return_url: str = "heishi://payout/connect/return"
    connect_refresh_url: str = "heishi://payout/connect/refresh"
    paypal_client_id: str = ""
    paypal_client_secret: str = ""
    alipay_app_id: str = ""
    alipay_private_key: str = ""
    alipay_public_key: str = ""
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
    def cors_origin_list(self) -> list[str]:
        if self.cors_origins.strip() == "*":
            return ["*"]
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
