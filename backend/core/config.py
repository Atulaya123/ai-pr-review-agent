import os
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
    # raw PEM content, for hosts with an ephemeral filesystem (e.g. Render) —
    # takes precedence over github_private_key_path when set.
    github_private_key: str | None = None

    # hitl gate
    hitl_confidence_threshold: float = 0.75

    # economics
    daily_budget_usd: float = 5.0

    webhook_max_body_bytes: int = 2_000_000

    # observability — LLM-call/agent-execution tracing, complements agent_events
    # (which is the business-level audit/cost ledger; LangSmith is execution-level
    # tracing of the LangGraph run itself: per-node latency, tokens, errors).
    langsmith_tracing: bool = False
    langsmith_api_key: str | None = None
    langsmith_project: str = "aipr-review-agent"


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    # LangGraph/LangChain's tracing client reads these from the real process
    # environment, not from this Settings object — pydantic-settings parses
    # .env internally without exporting it to os.environ, so this export is
    # what actually turns tracing on. Set both var-name generations (LangChain
    # renamed LANGCHAIN_* to LANGSMITH_* over time; older/newer SDK versions
    # may read either).
    if settings.langsmith_tracing and settings.langsmith_api_key:
        for prefix in ("LANGCHAIN", "LANGSMITH"):
            os.environ.setdefault(f"{prefix}_TRACING_V2", "true")
            os.environ.setdefault(f"{prefix}_TRACING", "true")
            os.environ.setdefault(f"{prefix}_API_KEY", settings.langsmith_api_key)
            os.environ.setdefault(f"{prefix}_PROJECT", settings.langsmith_project)
    return settings
