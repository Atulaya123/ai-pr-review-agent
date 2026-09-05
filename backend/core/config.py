import os
from functools import lru_cache

from pydantic import Field
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
    gemini_api_key: str | None = None
    # default matches the free local path (Ollama + nomic-embed-text, 768 dims).
    # The deployed instance sets EMBEDDING_PROVIDER=gemini + EMBEDDING_MODEL=
    # gemini-embedding-001 (Groq, its LLM provider, has no embeddings API, and
    # Gemini's free tier needs no payment method, unlike OpenAI's) — embedder.py
    # truncates the hosted providers' native output to embedding_dims via
    # output_dimensionality (Gemini) / dimensions (OpenAI), so this stays 768
    # and code_chunks.embedding never needs a schema change to switch providers.
    embedding_model: str = "nomic-embed-text"
    embedding_dims: int = 768
    embedding_provider: str = "ollama"  # "ollama" | "gemini" | "openai"

    # github app
    github_app_id: str | None = None
    github_webhook_secret: str | None = None
    github_private_key_path: str | None = None
    # raw PEM content, for hosts with an ephemeral filesystem (e.g. Render) —
    # takes precedence over github_private_key_path when set.
    github_private_key: str | None = None

    # hitl gate
    # Calibrated against 12 real findings across 4 live PRs (#9-#12) reviewed
    # by the deployed model (Groq openai/gpt-oss-120b) — every self-reported
    # confidence observed landed in [0.90, 0.99]. The original 0.75 default
    # was so far below that floor it was effectively dead code: nothing this
    # model reports would ever escalate. 0.93 sits strictly above the one
    # low-confidence outlier actually observed (0.90, a hedged LOW-severity
    # finding) and strictly below the more typical 0.95-0.99 band, so it can
    # still catch a genuine hedge without escalating routine confident
    # findings. Small sample, one model, one session — not a substitute for
    # tuning against real hitl_feedback dispute-rate data once that exists,
    # but a real improvement over an untested guess.
    hitl_confidence_threshold: float = 0.93

    # economics
    daily_budget_usd: float = 5.0

    webhook_max_body_bytes: int = 2_000_000

    # observability — LLM-call/agent-execution tracing, complements agent_events
    # (which is the business-level audit/cost ledger; LangSmith is execution-level
    # tracing of the LangGraph run itself: per-node latency, tokens, errors).
    # AIPR_LANGSMITH_TRACING, not LANGSMITH_TRACING: the LangSmith SDK itself
    # reads LANGSMITH_TRACING/LANGCHAIN_TRACING_V2 directly from the real process
    # environment (see langsmith/utils.py's get_env_var, namespaces=("LANGSMITH",
    # "LANGCHAIN")) — using that exact name here meant Render setting
    # LANGSMITH_TRACING=true self-activated the SDK's tracing even with no
    # LANGSMITH_API_KEY ever populated, bypassing the tracing-AND-key gate in
    # _langsmith_env_vars() below entirely and 401ing on every single LLM call.
    langsmith_tracing: bool = Field(default=False, validation_alias="AIPR_LANGSMITH_TRACING")
    langsmith_api_key: str | None = None
    langsmith_project: str = "aipr-review-agent"


def _langsmith_env_vars(settings: Settings) -> dict[str, str]:
    """Pure computation, no side effects — kept separate from get_settings()
    so tests can verify the mapping without ever actually flipping the real
    LANGCHAIN_TRACING_V2 env var. Doing that for real, even with a fake key
    and even briefly, was proven to leak: LangChain's tracing client buffers
    and flushes on a background thread at process exit, independent of
    whether the env var got cleaned up long before that point — a fake key
    from a test can still produce a real (if harmless, 403) HTTP call to
    api.smith.langchain.com after the test that set it has already finished.
    """
    if not (settings.langsmith_tracing and settings.langsmith_api_key):
        return {}
    env: dict[str, str] = {}
    for prefix in ("LANGCHAIN", "LANGSMITH"):
        env[f"{prefix}_TRACING_V2"] = "true"
        env[f"{prefix}_TRACING"] = "true"
        env[f"{prefix}_API_KEY"] = settings.langsmith_api_key
        env[f"{prefix}_PROJECT"] = settings.langsmith_project
    return env


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    # LangGraph/LangChain's tracing client reads these from the real process
    # environment, not from this Settings object — pydantic-settings parses
    # .env internally without exporting it to os.environ, so this export is
    # what actually turns tracing on.
    for key, value in _langsmith_env_vars(settings).items():
        os.environ.setdefault(key, value)
    return settings
