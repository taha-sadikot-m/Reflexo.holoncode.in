"""Application settings (POC_01 §8). Loaded from environment variables prefixed REFLEXO_ and from `.env`."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="REFLEXO_", env_file=".env", extra="ignore")

    env: Literal["dev", "test", "demo"] = "dev"
    public_base_url: str = "http://localhost:8080"
    log_level: str = "INFO"

    # Datastores
    database_url: str = "postgresql+asyncpg://reflexo_app:app_dev@localhost:5442/reflexo"
    database_service_url: str = "postgresql+asyncpg://reflexo_service:service_dev@localhost:5442/reflexo"
    database_owner_url: str = "postgresql+asyncpg://reflexo_owner:owner_dev@localhost:5442/reflexo"
    db_pool_size: int = 10
    db_statement_timeout_ms: int = 5000
    redis_url: str = "redis://localhost:6390/0"
    temporal_address: str = "localhost:7243"

    # Observability
    otel_exporter_otlp_endpoint: str | None = "http://localhost:4317"
    otel_sampling_ratio: float = 1.0

    # Demo controls
    demo_time_scale: float = Field(default=1.0, gt=0, le=1.0)

    # Pipeline (POC_03 §13)
    coalesce_window_ms: int = 1500
    coalesce_short_ms: int = 600
    coalesce_cap_ms: int = 4000
    turn_lease_s: int = 20
    turn_max_attempts: int = 3
    turn_business_concurrency: int = 4
    holding_after_s: int = 12
    outbound_rate_per_s: float = 20.0
    outbound_unknown_wait_s: int = 10
    queue_batch: int = 10
    poll_interval_s: float = 1.0

    # Admin panel
    admin_user: str = "admin"
    admin_password: SecretStr = SecretStr("change-me")

    # Seed identities (used by scripts/seed.py; real values come from .env)
    seed_owner_wa_e164: str = "+919999900000"
    seed_owner_tg_user_id: str = "0"
    seed_wa_phone_number_id: str = "unset-wa-phone-number-id"
    seed_wa_display_number: str = "+910000000000"
    seed_tg_bot_id: str = "unset-telegram-bot-id"
    seed_fb_page_id: str = "unset-fb-page-id"
    seed_ig_account_id: str = "unset-ig-account-id"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
