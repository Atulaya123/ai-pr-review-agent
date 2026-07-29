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
    llm_provider: str = "mock"  # "openai" | "anthropic" | "ollama" | "groq" | "mock"
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    # free hosted inference for the deployed instance — local Ollama can't run
    # on free-tier hosting (not enough RAM for model weights).
    groq_api_key: str | None = None
    # default matches the free local path (Ollama + nomic-embed-text, 768 dims).
    # Swap to "text-embedding-3-large" (256 dims, per the architecture doc) if
    # running on OpenAI — code_chunks.embedding's column width must match.
    embedding_model: str = "nomic-embed-text"
    embedding_dims: int = 768
    embedding_provider: str = "ollama"  # "ollama" | "openai"

    # github app
    github_app_id: str | None = None
    github_webhook_secret: str | None = None
    github_private_key_path: str | None = None

    # hitl gate
    hitl_confidence_threshold: float = 0.75

    # economics
    daily_budget_usd: float = 5.0

    webhook_max_body_bytes: int = 2_000_000

    # alerting
    slack_webhook_url: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
