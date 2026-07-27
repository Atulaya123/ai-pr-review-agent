from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # database
    # NOTE: 5544/6480 (not 5432/6379) — this dev machine already has other
    # services bound to the standard ports; docker-compose.yml maps to these.
    tiger_database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5544/aipr_review"

    # queue
    redis_url: str = "redis://localhost:6480/0"

    # llm
    llm_provider: str = "mock"  # "openai" | "anthropic" | "mock"
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    embedding_model: str = "text-embedding-3-large"
    embedding_dims: int = 256

    # github app
    github_app_id: str | None = None
    github_webhook_secret: str | None = None
    github_private_key_path: str | None = None

    # hitl gate
    hitl_confidence_threshold: float = 0.75

    # economics
    daily_budget_usd: float = 5.0

    webhook_max_body_bytes: int = 2_000_000


@lru_cache
def get_settings() -> Settings:
    return Settings()
