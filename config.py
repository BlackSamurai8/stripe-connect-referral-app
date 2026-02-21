"""
Application configuration loaded from environment variables.
"""

from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # Stripe
    stripe_secret_key: str
    stripe_publishable_key: str
    stripe_webhook_secret: str
    stripe_connect_webhook_secret: str

    # GoHighLevel
    ghl_api_key: str
    ghl_webhook_secret: str = ""

    # Database
    database_url: str = "sqlite:///./referral_app.db"

    # App
    app_secret_key: str = "change-me-in-production"
    app_base_url: str = "http://localhost:8000"
    frontend_url: str = "http://localhost:3000"

    # Commission defaults
    default_max_depth: int = 5

    # Observability
    log_level: str = "INFO"
    sentry_dsn: str = ""

    # Dead letter queue
    dlq_max_retries: int = 3
    dlq_retry_delay_minutes: int = 15

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
