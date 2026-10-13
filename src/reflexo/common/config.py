"""Application settings (POC_01 §8). Loaded from environment variables prefixed REFLEXO_ and from `.env`."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal, Self

from pydantic import Field, SecretStr, model_validator
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
    temporal_namespace: str = "default"
    temporal_task_queue: str = "reflexo-main"  # tests use their own queue so a dev worker never picks them up
    guard_timeout_ms: int = 1000  # spec 200 ms; laptop Docker latency varies and a timeout fails closed

    # Observability
    otel_exporter_otlp_endpoint: str | None = "http://localhost:4317"
    otel_sampling_ratio: float = 1.0

    # Demo controls
    demo_time_scale: float = Field(default=1.0, gt=0, le=1.0)
    # Demo time compresses calendar time, not people: a window for a customer or owner to respond ("reply YES within
    # 2 hours") is scaled but never shorter than this (live: 2 h became 5 s and the owner was alerted before the
    # customer could read the message). Timers ("1 day before delivery") stay fully scaled.
    demo_response_window_min_s: int = 90

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

    # Gateway (POC_03 §13)
    human_takeover_min: int = 30
    automation_daily_cap: int = 3
    link_code_ttl_min: int = 30
    media_dir: str = "./data/media"
    media_url_secret: SecretStr = SecretStr("dev-media-secret-change-me")
    media_url_ttl_s: int = 3600
    max_webhook_bytes: int = 1_048_576

    # Meta (WhatsApp Cloud API, Messenger, Instagram)
    meta_app_id: str = ""
    meta_app_secret: SecretStr = SecretStr("")
    meta_verify_token: SecretStr = SecretStr("")
    meta_graph_version: str = "v23.0"
    meta_graph_base: str = "https://graph.facebook.com"
    instagram_graph_base: str = "https://graph.instagram.com"
    wa_access_token: SecretStr = SecretStr("")
    wa_display_number: str = ""
    wa_order_update_template: str = "order_update"
    fb_page_token: SecretStr = SecretStr("")
    ig_access_token: SecretStr = SecretStr("")
    ig_app_secret: SecretStr = SecretStr("")  # Instagram Login apps sign with the Instagram app secret (verify)

    # Telegram
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_webhook_secret: SecretStr = SecretStr("")
    telegram_api_base: str = "https://api.telegram.org"

    # Simulator (signed provider-shaped payloads from the Simulation Lab)
    simulator_secret: SecretStr = SecretStr("dev-simulator-secret")

    # model gateway (POC_04 §2)
    model_provider: Literal["gemini", "fake"] = "gemini"
    google_api_key: SecretStr = SecretStr("")
    model_primary: str = "gemini-2.5-flash"
    model_fallback: str = "gemini-2.5-flash-lite"
    embedding_model: str = "gemini-embedding-001"
    models_config: str = "config/models.yaml"

    # admin panel (POC_07 §1)
    admin_user: str = "admin"
    admin_password: SecretStr = SecretStr("admin-dev-password")
    admin_session_secret: SecretStr = SecretStr("dev-admin-session-secret")
    admin_session_hours: int = 8

    # public demo (/demo): every visitor gets a private copy of the demo shop, deleted after demo_sandbox_hours
    demo_enabled: bool = True
    demo_sandbox_hours: int = 24
    demo_daily_cap: int = 300  # new demo shops per day, across everyone
    demo_per_client_per_hour: int = 6  # new demo shops per client address per hour

    # commerce / Razorpay (POC_05 §4.5, §6)
    razorpay_key_id: str = ""
    razorpay_key_secret: SecretStr = SecretStr("")
    razorpay_webhook_secret: SecretStr = SecretStr("")
    razorpay_api_base: str = "https://api.razorpay.com/v1"
    payment_link_ttl_h: int = 48
    razorpay_reconcile_s: int = 120  # demo: check unpaid links every 2 min (missed webhooks)

    # voice (POC_04 §10, §13)
    livekit_url: str = ""
    livekit_public_url: str = ""  # what browsers connect to (wss://...); defaults to livekit_url
    livekit_api_key: str = ""
    livekit_api_secret: SecretStr = SecretStr("")
    deepgram_api_key: SecretStr = SecretStr("")
    stt_model: str = "nova-3"
    stt_language: str = "multi"
    tts_model: str = "aura-2-asteria-en"
    voice_model: str = "gemini-2.5-flash-lite"  # streaming voice LLM (LiveKit Google plugin)
    voice_agent_name: str = "reflexo-voice"
    voice_agent_join_timeout_s: float = 10.0  # wait for the agent's audio before Graph `accept`
    voice_endpointing_min_s: float = 0.45
    voice_endpointing_max_s: float = 2.0
    voice_silence_hangup_s: int = 30
    voice_max_concurrent_calls: int = 5

    # desks (POC_04 §13)
    agent_checkpointer: Literal["postgres", "memory"] = "postgres"
    max_model_calls_customer: int = 4
    max_model_calls_owner: int = 6  # owner turns chain lookups + a change + the reply (POC_04 §9)
    max_tool_calls_owner: int = 10
    max_tool_calls_customer: int = 8

    # knowledge / search (POC_05 §6)
    search_k: int = 5
    search_trgm_threshold: float = Field(default=0.3, ge=0.05, le=1.0)
    # Ceiling, not a target: prepare waits for the other context loads anyway (~200 ms in a live turn), and a miss
    # costs a whole extra model call. Live: a 120 ms cap timed out at 125 ms while prepare took 209 ms.
    speculative_budget_ms: int = 800

    # orders
    draft_idle_hours: int = 12  # an unfinished draft untouched this long is abandoned; a new chat starts fresh

    # Seed identities (used by scripts/seed.py; real values come from .env)
    seed_owner_wa_e164: str = "+919999900000"
    seed_owner_tg_user_id: str = "0"
    seed_wa_phone_number_id: str = "unset-wa-phone-number-id"
    seed_wa_display_number: str = "+910000000000"
    seed_tg_bot_id: str = "unset-telegram-bot-id"
    seed_fb_page_id: str = "unset-fb-page-id"
    seed_ig_account_id: str = "unset-ig-account-id"

    @model_validator(mode="after")
    def _deployed_secrets_are_real(self) -> Self:
        """A deployed stack (env=demo) must not run with dev/placeholder signing secrets: anyone could forge
        simulator events or media URLs."""
        if self.env != "demo":
            return self
        weak = [
            name
            for name in ("media_url_secret", "simulator_secret", "admin_password", "admin_session_secret")
            if len(v := getattr(self, name).get_secret_value()) < 16 or "change-me" in v or v.startswith("dev-")
        ]
        if weak:
            raise ValueError(f"set strong values (>= 16 chars) for: {', '.join('REFLEXO_' + w.upper() for w in weak)}")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
