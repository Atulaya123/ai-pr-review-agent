-- Versioned model prices and per-attempt cost reservations/reconciliation.
-- Safe to apply repeatedly. Run against TIGER_DATABASE_URL before deploying
-- the budgeted LLM client.

CREATE TABLE IF NOT EXISTS price_versions (
    id                          UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    provider                    TEXT            NOT NULL,
    model                       TEXT            NOT NULL,
    input_rate_per_million      NUMERIC(20, 12) NOT NULL,
    cached_input_rate_per_million NUMERIC(20, 12) NOT NULL,
    output_rate_per_million     NUMERIC(20, 12) NOT NULL,
    currency                    VARCHAR(3)      NOT NULL DEFAULT 'USD',
    effective_from              DATE            NOT NULL,
    source                      TEXT            NOT NULL,
    created_at                  TIMESTAMPTZ     NOT NULL DEFAULT now(),
    CONSTRAINT uq_price_versions_version UNIQUE (provider, model, effective_from)
);

CREATE TABLE IF NOT EXISTS model_calls (
    id                      UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at              TIMESTAMPTZ     NOT NULL,
    provider                TEXT            NOT NULL,
    model                   TEXT            NOT NULL,
    price_version_id        UUID            NOT NULL REFERENCES price_versions(id),
    purpose                 TEXT            NOT NULL DEFAULT 'agent',
    review_id               UUID,
    agent                   TEXT,
    tokens_in               INTEGER,
    tokens_out              INTEGER,
    cached_input_tokens     INTEGER,
    input_tokens_estimated  BOOLEAN         NOT NULL DEFAULT FALSE,
    output_tokens_estimated BOOLEAN         NOT NULL DEFAULT FALSE,
    raw_usage               JSONB,
    cost_usd                NUMERIC(20, 12),
    reserved_cost_usd       NUMERIC(20, 12) NOT NULL,
    status                  TEXT            NOT NULL DEFAULT 'reserved'
);

CREATE INDEX IF NOT EXISTS ix_model_calls_created_at ON model_calls (created_at);
CREATE INDEX IF NOT EXISTS ix_model_calls_price_version_id ON model_calls (price_version_id);
CREATE INDEX IF NOT EXISTS ix_model_calls_review_id ON model_calls (review_id);

INSERT INTO price_versions
    (provider, model, input_rate_per_million, cached_input_rate_per_million,
     output_rate_per_million, currency, effective_from, source)
VALUES
    ('groq', 'openai/gpt-oss-120b', 0.15, 0.075, 0.60, 'USD', DATE '2026-11-04',
     'https://console.groq.com/docs/model/openai/gpt-oss-120b'),
    ('mock', 'mock', 0, 0, 0, 'USD', DATE '2026-11-04',
     'Deterministic local MockLLMClient; no provider-billed token usage.'),
    ('ollama', 'qwen2.5:14b-instruct', 0, 0, 0, 'USD', DATE '2026-11-04',
     'Local Ollama inference; no provider-billed token usage.'),
    ('gemini', 'gemini-embedding-001', 0, 0, 0, 'USD', DATE '2026-11-04',
     'Gemini API free tier; embedding use is currently within the free-tier allowance.')
ON CONFLICT (provider, model, effective_from) DO NOTHING;
