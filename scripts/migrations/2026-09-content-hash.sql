-- M3: content-hash idempotency for the Airflow knowledge-base-refresh DAG
-- (airflow/dags/knowledge_base_refresh.py). Idempotent — safe to re-run.
--
-- content_hash is a generated column (derived from content, never written
-- directly) so the DAG's embed_batch task can compare Postgres's md5(content)
-- against a locally-computed md5 of the same chunk text before deciding
-- whether to spend an embedding-API call on it — the point isn't just
-- avoiding duplicate rows (the existing UNIQUE (repo, path, chunk_index)
-- already does that), it's avoiding duplicate *work* on unchanged content.

ALTER TABLE code_chunks
    ADD COLUMN IF NOT EXISTS content_hash TEXT
        GENERATED ALWAYS AS (md5(content)) STORED;

CREATE INDEX IF NOT EXISTS code_chunks_hash_idx
    ON code_chunks (repo, path, chunk_index, content_hash);
