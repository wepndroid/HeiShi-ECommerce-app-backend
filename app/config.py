from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./heishi.db"
    jwt_secret: str = "dev-secret-change-in-production"
    jwt_access_expire_seconds: int = 3600
    jwt_refresh_expire_days: int = 30
    base_url: str = "http://127.0.0.1:8000"
    cors_origins: str = "*"
    upload_dir: str = "uploads"
    escrow_fee: float = 0.99
    pending_pay_expire_minutes: int = 30
    expose_dev_otp: bool = True

    # Supabase Auth (Path A — phone OTP). When jwt_secret is set, API accepts Supabase JWTs.
    supabase_url: str = ""
    supabase_jwt_secret: str = ""
    supabase_service_role_key: str = ""

    @property
    def supabase_auth_enabled(self) -> bool:
        return bool(self.supabase_jwt_secret.strip())

    @property
    def cors_origin_list(self) -> list[str]:
        if self.cors_origins.strip() == "*":
            return ["*"]
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
