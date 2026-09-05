-- M2: run this against TIGER_DATABASE_URL once Tiger Cloud credentials exist.
-- Adds the memory lane (pgvectorscale/DiskANN) and upgrades agent_events to a
-- real hypertable + continuous aggregates, per pr-review-agent.html Part II/2.3.
-- Idempotent — safe to re-run.
--
-- Embedding width is 768 to match the free local path (Ollama + nomic-embed-text).
-- The deployed instance uses EMBEDDING_PROVIDER=gemini (gemini-embedding-001)
-- but embedder.py passes output_dimensionality=768 to Gemini's embed_content
-- call (dimensions=768 for OpenAI, the other supported hosted option),
-- truncating native output down to match this column — so VECTOR(768) is
-- correct for all three providers and no ALTER COLUMN is needed when
-- switching between them. Switching providers still requires re-running
-- scripts/ingest_docs.py so the stored vectors live in the same embedding
-- space as the query-time vectors.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS vectorscale;
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ── Lane 1: Memory ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS code_chunks (
    id           UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    repo         TEXT         NOT NULL,
    path         TEXT         NOT NULL,
    symbol       TEXT,
    chunk_index  INT          NOT NULL,
    content      TEXT         NOT NULL,
    embedding    VECTOR(768)  NOT NULL,
    token_count  INT,
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS code_chunks_unique_idx
    ON code_chunks (repo, path, chunk_index);

CREATE INDEX IF NOT EXISTS code_chunks_emb_idx
    ON code_chunks USING diskann (embedding vector_cosine_ops);

ALTER TABLE code_chunks
    ADD COLUMN IF NOT EXISTS content_tsv TSVECTOR
        GENERATED ALWAYS AS (to_tsvector('english', content)) STORED;

CREATE INDEX IF NOT EXISTS code_chunks_fts_idx
    ON code_chunks USING GIN (content_tsv);

CREATE TABLE IF NOT EXISTS repo_file_index (
    repo            TEXT        NOT NULL,
    path            TEXT        NOT NULL,
    last_indexed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    content_hash    TEXT        NOT NULL,
    PRIMARY KEY (repo, path)
);

-- ── Lane 2: Time — upgrade agent_events (created by scripts/init_db.py) to a hypertable ──
-- TimescaleDB requires any unique constraint on a hypertable to include the
-- partitioning column. agent_events.id is a plain single-column UUID PK from
-- the M1 SQLAlchemy model (fine for a non-hypertable), so it must be dropped
-- before conversion — this is an append-only events table, not referenced by
-- any foreign key, so id no longer needs a uniqueness guarantee.
ALTER TABLE agent_events DROP CONSTRAINT IF EXISTS agent_events_pkey;

-- Only run this the first time; create_hypertable errors if already converted.
SELECT create_hypertable('agent_events', by_range('ts', INTERVAL '1 day'), if_not_exists => TRUE, migrate_data => TRUE);

-- ── Lane 3: Rollups ─────────────────────────────────────────────────────────
CREATE MATERIALIZED VIEW IF NOT EXISTS agent_health_1m
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 minute', ts)                          AS bucket,
    agent,
    count(*) FILTER (WHERE event_type = 'llm.call')      AS llm_calls,
    sum(cost_usd)                                        AS cost_usd,
    approx_percentile(0.95, percentile_agg(latency_ms))  AS p95_ms,
    count(*) FILTER (WHERE outcome = 'rejected')::float
        / NULLIF(count(*) FILTER (WHERE outcome IS NOT NULL), 0) AS rejection_rate
FROM agent_events
GROUP BY bucket, agent
WITH NO DATA;

SELECT add_continuous_aggregate_policy(
    'agent_health_1m',
    start_offset      => INTERVAL '2 hours',
    end_offset        => INTERVAL '1 minute',
    schedule_interval => INTERVAL '1 minute',
    if_not_exists     => TRUE
);

CREATE MATERIALIZED VIEW IF NOT EXISTS pr_cost_hourly
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 hour', ts)   AS bucket,
    review_id,
    sum(cost_usd)               AS total_cost_usd,
    count(DISTINCT agent)       AS agents_used,
    max(confidence)             AS max_confidence
FROM agent_events
GROUP BY bucket, review_id
WITH NO DATA;

SELECT add_continuous_aggregate_policy(
    'pr_cost_hourly',
    start_offset      => INTERVAL '1 day',
    end_offset        => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour',
    if_not_exists     => TRUE
);
