"""Nightly knowledge-base refresh for the RAG lane (backend/memory/).

    fetch_repo_docs -> parse_and_chunk -> embed_batch -> upsert_index -> run_eval -> gate

Turns what was a manual `python -m scripts.ingest_docs` + `python -m scripts.run_eval`
into a scheduled, observable, retryable pipeline over the same underlying code —
this DAG does not reimplement ingestion or eval, it orchestrates the existing
scripts/ingest_docs.py source list and backend/evaluation/runner.py functions.

Design decisions (each is deliberate, not default Airflow behaviour):

* Idempotency — chunks are keyed on a content hash (scripts/migrations/
  2026-09-content-hash.sql), not just on (repo, path, chunk_index). The unique
  index already stops duplicate rows; the hash stops duplicate *work* — a
  chunk whose content hasn't changed since the last run never gets re-sent to
  the embeddings API. That matters once there's a per-call cost or a rate
  limit, not just for correctness.
* XCom discipline — tasks pass a staging-file path (or a row count), never
  chunk content or embedding vectors. XCom is metadata storage, not a data bus.
* Retries — only embed_batch retries (it's the one external API call in the
  chain); everything else either succeeds deterministically or should fail
  loudly rather than mask an upstream bug in a retry loop.
* The eval gate is retrieval-only by design: recall@3 and MRR are pure
  vector-math over what this run just ingested, so a drop is a real signal
  about the change. The generation metrics (faithfulness/relevance) run
  against a fixed adversarial fixture (backend/evaluation/dataset.py's
  GENERATION_TEST_SET) that has nothing to do with today's ingested content —
  gating the DAG on that would be gating on LLM-as-judge noise, not on
  anything this run touched. They're still computed and logged every run so a
  real regression in judge behaviour is visible, just not fatal.
* catchup=False — the source is "current state of this repo's architecture
  docs", not a time-partitioned dataset. Backfilling would re-embed the same
  six invariants N times for identical content (which the hash check would
  immediately no-op anyway) — there's no historical interval here to recover.

Known limitation: content_hash is derived from chunk *text* only (see the
2026-09 migration), not from EMBEDDING_PROVIDER/EMBEDDING_MODEL. Triggering
this DAG immediately after switching providers would incorrectly skip
re-embedding every chunk, since the six paragraphs' text didn't change even
though the target embedding space did — verified by hand when moving this
project's embeddings from Ollama to Gemini (recall@3 stayed 1.00, MRR moved
from 0.889 to 1.000 on the same live database and test set, but only because
`scripts/ingest_docs.py` was run directly, bypassing this DAG's skip check
entirely). A provider switch needs a manual `ingest_docs.py` run first, or
this DAG's skip check would need to key on (content, provider, model), not
content alone — not implemented, since provider switches are rare and manual
here, not something this DAG needs to detect on its own yet.

Known scope limits (see docs/INTERVIEW_PREP.md's chunking-gap note): fetch_repo_docs
reads the same hardcoded CHUNKS list scripts/ingest_docs.py already uses, it does not
clone the repo or parse markdown/AST — swapping that source is the natural next step
and none of the tasks downstream of it would need to change.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import Any

from airflow.exceptions import AirflowFailException
from airflow.sdk import dag, task
from pendulum import datetime as pendulum_datetime

from backend.core.config import get_settings
from backend.evaluation.runner import compute_generation_metrics, compute_retrieval_metrics
from backend.memory.embedder import embed_text
from backend.memory.tiger_client import ChunkRecord, fetch_existing_hashes, upsert_chunks
from scripts.ingest_docs import CHUNKS, REPO

RECALL_AT_3_THRESHOLD = 0.8
MRR_THRESHOLD = 0.5
STAGING_ROOT = Path(tempfile.gettempdir()) / "aipr_kb_refresh"


def _run(coro: Any) -> Any:
    """TaskFlow callables are sync; the project's DB/HTTP/embedding calls are
    all async (backend/memory, backend/evaluation) — one bridge point."""
    return asyncio.run(coro)


def _content_hash(content: str) -> str:
    """Matches Postgres's md5(content), which is what content_hash (a
    generated column, see the 2026-09 migration) actually stores — comparing
    the two tells embed_batch whether a chunk changed since the last run."""
    return hashlib.md5(content.encode("utf-8")).hexdigest()


@dag(
    dag_id="knowledge_base_refresh",
    description="Nightly re-embed of this repo's architecture invariants into code_chunks",
    schedule="@daily",
    start_date=pendulum_datetime(2026, 9, 1, tz="UTC"),
    catchup=False,
    default_args={"retries": 0, "owner": "aipr-review-agent"},
    tags=["rag", "knowledge-base"],
)
def knowledge_base_refresh() -> None:
    @task
    def fetch_repo_docs(run_id: str) -> str:
        # `run_id` isn't passed anywhere below — TaskFlow auto-injects it from
        # the DAG run context because the parameter name matches a context
        # key, giving each run its own staging directory for free.
        """Stage this run's source chunks to disk; XCom below carries only the
        file path, never the six paragraphs of content themselves."""
        run_dir = STAGING_ROOT / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        raw_path = run_dir / "raw_chunks.json"
        raw_path.write_text(
            json.dumps(
                [
                    {"path": path, "symbol": symbol, "chunk_index": i, "content": content}
                    for i, (path, symbol, content) in enumerate(CHUNKS)
                ]
            )
        )
        return str(raw_path)

    @task
    def parse_and_chunk(raw_path: str) -> str:
        """CHUNKS is already pre-split (one chunk per invariant) — this task's
        real job today is computing the content hash each downstream task
        keys idempotency on. A real chunking strategy (splitting actual source
        files, not hand-written paragraphs) slots in here without touching
        embed_batch/upsert_index at all — see docs/INTERVIEW_PREP.md."""
        raw_chunks = json.loads(Path(raw_path).read_text())
        for chunk in raw_chunks:
            chunk["content_hash"] = _content_hash(chunk["content"])

        run_dir = Path(raw_path).parent
        hashed_path = run_dir / "hashed_chunks.json"
        hashed_path.write_text(json.dumps(raw_chunks))
        return str(hashed_path)

    @task(
        retries=2,
        retry_delay=timedelta(seconds=30),
        retry_exponential_backoff=True,
        max_retry_delay=timedelta(minutes=5),
    )
    def embed_batch(hashed_path: str) -> str:
        """The one task that calls an external API (Ollama locally, or
        whichever EMBEDDING_PROVIDER is configured) — the only task with
        retries. Chunks whose hash matches what's already stored are skipped
        entirely, not just deduped at write time."""
        settings = get_settings()
        chunks = json.loads(Path(hashed_path).read_text())
        existing_hashes = _run(fetch_existing_hashes(REPO))

        embedded: list[dict] = []
        skipped = 0
        for chunk in chunks:
            key = (chunk["path"], chunk["chunk_index"])
            if existing_hashes.get(key) == chunk["content_hash"]:
                skipped += 1
                continue
            chunk["embedding"] = _run(embed_text(chunk["content"], settings))
            embedded.append(chunk)

        print(f"embed_batch: {len(embedded)} embedded, {skipped} unchanged/skipped")

        run_dir = Path(hashed_path).parent
        embedded_path = run_dir / "embedded_chunks.json"
        embedded_path.write_text(json.dumps(embedded))
        return str(embedded_path)

    @task
    def upsert_index(embedded_path: str) -> int:
        """Idempotent upsert (ON CONFLICT DO UPDATE on the existing
        (repo, path, chunk_index) unique index) — re-running this DAG for
        unchanged content is a safe no-op, not a duplicate insert."""
        chunks = json.loads(Path(embedded_path).read_text())
        records = [
            ChunkRecord(
                path=c["path"],
                symbol=c["symbol"],
                chunk_index=c["chunk_index"],
                content=c["content"],
                embedding=c["embedding"],
            )
            for c in chunks
        ]
        if not records:
            print("upsert_index: nothing changed, 0 rows written")
            return 0
        return _run(upsert_chunks(REPO, records))

    @task
    def run_eval(_upserted_count: int) -> dict:
        """Depends on upsert_index only to guarantee ordering (code_chunks
        must reflect this run before eval queries it) — the row count itself
        isn't used. Returns a small numeric summary via XCom, not raw
        retrieved chunk content."""
        settings = get_settings()
        retrieval = _run(compute_retrieval_metrics(settings))
        generation = _run(compute_generation_metrics(settings))
        return {
            "recall_at_3": retrieval.mean_recall_at_3,
            "mrr": retrieval.mrr,
            "faithfulness_rate": generation.faithfulness_rate,
            "relevance_rate": generation.relevance_rate,
        }

    @task
    def gate(metrics: dict) -> None:
        """Fails the DAG run on retrieval regressions only — see the module
        docstring for why generation metrics are logged, not gated on."""
        print(
            f"recall@3={metrics['recall_at_3']:.2f} mrr={metrics['mrr']:.3f} "
            f"faithfulness={metrics['faithfulness_rate']:.2f} relevance={metrics['relevance_rate']:.2f}"
        )
        if metrics["recall_at_3"] < RECALL_AT_3_THRESHOLD:
            raise AirflowFailException(
                f"recall@3 {metrics['recall_at_3']:.2f} below threshold {RECALL_AT_3_THRESHOLD}"
            )
        if metrics["mrr"] < MRR_THRESHOLD:
            raise AirflowFailException(f"MRR {metrics['mrr']:.3f} below threshold {MRR_THRESHOLD}")

    raw = fetch_repo_docs()
    hashed = parse_and_chunk(raw)
    embedded = embed_batch(hashed)
    upserted = upsert_index(embedded)
    metrics = run_eval(upserted)
    gate(metrics)


knowledge_base_refresh()
