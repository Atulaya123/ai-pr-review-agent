# AI PR Review Agent

**Live and publicly triggerable: https://aipr-review-agent.onrender.com** — fork this repo,
open a PR against it, and watch four AI specialists review it for real. No laptop required;
see [example #5](https://github.com/Atulaya123/ai-pr-review-agent/pull/5) for a real run.

A production-grade AI pull-request review agent: four specialist reasoners (security,
quality, tests, docs) fan out over a diff in parallel via LangGraph, each grounded in
retrieved codebase context, merged by a confidence-weighted HITL aggregator, with every
action written to an events spine. Full design rationale in [`pr-review-agent.html`](pr-review-agent.html).

Built with the [genesis-kit](https://github.com/ayush488-glitch/genesis-kit) ritual — see
[`.genesis/`](.genesis/) for the cognitive design, invariants, and milestone plan this was
built against.

**Docs:** [`docs/LEARN_THE_STACK.md`](docs/LEARN_THE_STACK.md) (start here if any tool in this
repo is unfamiliar — a from-zero primer on every technology used, in plain English) ·
[`docs/SETUP.md`](docs/SETUP.md) (step-by-step credential setup, local + free deployment) ·
[`docs/ARCHITECTURE_DECISIONS.md`](docs/ARCHITECTURE_DECISIONS.md) (tech stack & design
trade-offs, with what was given up) · [`docs/INTERVIEW_PREP.md`](docs/INTERVIEW_PREP.md)
(Q&A defense of this project, including real bugs found while building it)

## Status

**Live and deployed.** The full pipeline (webhook → queue → 4-agent LangGraph fan-out →
confidence-weighted HITL gate → posted review) runs on Render's free tier using Groq's free
API (`openai/gpt-oss-120b`) for reasoning — genuinely triggerable by anyone, not just a
local demo. RAG grounding runs on the deployed instance too: Groq has no embeddings API, so
`EMBEDDING_PROVIDER=gemini` (`gemini-embedding-001`, truncated to 768 dims to match
`code_chunks.embedding`) covers just that one call, on Gemini's no-card free tier — Groq
still does all the LLM reasoning. `LLM_PROVIDER` and `EMBEDDING_PROVIDER` stay independently
configurable: clone the repo and run `ollama` locally for a zero-external-dependency path
instead, or set `EMBEDDING_PROVIDER=openai` if you'd rather pay for OpenAI's embeddings.
See `.genesis/PLAN.md` for remaining scope (M3 dashboard/economics, M4 fault-injection tests).

## Architecture at a glance

```
GitHub PR → FastAPI ingress (HMAC + idempotency) → Redis/ARQ queue → ARQ worker
  → LangGraph orchestrator ─┬─ security agent  ─┐
                            ├─ quality agent   ─┤→ aggregator (dedup, confidence, HITL gate) → GitHub review
                            ├─ tests agent     ─┤
                            └─ docs agent      ─┘
  → agent_events (business-level audit/cost ledger) + pr_review_records/finding_records (truth)
    in Postgres/Tiger Cloud, plus optional LangSmith tracing (execution-level: per-node
    latency/tokens/errors on the LangGraph run itself — LANGSMITH_TRACING=true)
```

## Run the M1 demo (no credentials needed)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt

docker compose up -d          # Postgres on :5544, Redis on :6480 (non-standard ports —
                               # see docker-compose.yml if 5432/6379 are already in use on your machine)
PYTHONPATH=. python -m scripts.init_db

PYTHONPATH=. python -m pytest backend/tests/ -v -c backend/pytest.ini
```

All 24 tests should pass, including `test_pipeline_flags_sql_injection_and_blocks` — the
real end-to-end path: a fixture diff with an unparameterized SQL query goes through the
actual LangGraph graph (mock LLM client, zero network calls) and comes back with a
`CRITICAL` finding and a `CRITICAL_BLOCK` outcome.

### Run it as a live server + worker

```bash
cp .env.example .env   # defaults already point at the docker-compose services

PYTHONPATH=. uvicorn backend.api.main:app --reload --port 8000   # terminal 1
PYTHONPATH=. arq backend.job_queue.arq_worker.WorkerSettings     # terminal 2
```

```bash
curl localhost:8000/health
# webhook requires a valid HMAC signature — see backend/tests/test_webhook_verify.py
# for how to construct one, or wait for M2's real GitHub App delivery.
curl localhost:8000/api/reviews/<review_id>   # after a job has run
```

## Getting to a live demo

Full step-by-step in `docs/SETUP.md`. Summary:

1. **GitHub App** (webhook + posting reviews) → `GITHUB_APP_ID`, `GITHUB_WEBHOOK_SECRET`,
   `GITHUB_PRIVATE_KEY_PATH` (local) or `GITHUB_PRIVATE_KEY` (deployed — raw PEM content,
   since free hosts have an ephemeral filesystem)
2. **Tiger Cloud** (the database — free trial, no card) → `TIGER_DATABASE_URL`, then run
   `scripts/migrations/2026-06-tiger-init.sql` against it. Its IP allowlist needs to permit
   whatever's connecting — your own IP for local dev, opened up entirely for a host with no
   static IP (most free tiers, including Render's)
3. **LLM**: `ollama` (free, local) or `groq` (free, hosted — what the deployed instance uses
   for reasoning) or `openai`/`anthropic`
4. **Embeddings** (separate from LLM choice — RAG grounding needs this too): `ollama` (free,
   local, `nomic-embed-text`) or `gemini` (`gemini-embedding-001`, free tier, no card — what
   the deployed instance uses, since Groq has no embeddings API of its own) or `openai`
   (`text-embedding-3-small`, paid)

Put local credentials in `.env` (gitignored) — never in source, never committed. Deployed
credentials go in the hosting platform's own environment variable UI (see `render.yaml`).

## Project layout

See `.genesis/DONE.html` section 4 and `pr-review-agent.html` section 4.2 for the full
module map. `backend/memory/` implements real retrieval (pgvector cosine search over
`code_chunks`) against this project's own architecture rules, ingested via
`scripts/ingest_docs.py`. Embeddings are provider-swappable independent of the LLM: local
runs use Ollama (`nomic-embed-text`), the deployed instance uses Gemini
(`gemini-embedding-001`, free tier) since Groq — its LLM provider — has no embeddings API.

## Nightly knowledge-base refresh (Airflow)

`scripts/ingest_docs.py` was a manual, one-shot script. `airflow/dags/knowledge_base_refresh.py`
turns the same ingestion into a scheduled, retryable, observable pipeline over that same
underlying code (it orchestrates the existing scripts/functions, it doesn't reimplement them):

```
fetch_repo_docs → parse_and_chunk → embed_batch → upsert_index → run_eval → gate
```

Runs `@daily` with `catchup=False` — the source is "current state of this repo's architecture
docs," not a time-partitioned dataset, so there's no historical interval to backfill; re-running
for a past date would just re-embed the same six invariants again.

**Idempotency** is keyed on a content hash, not just on `(repo, path, chunk_index)`. The existing
unique index already prevents duplicate *rows*; `scripts/migrations/2026-09-content-hash.sql`
adds a generated `content_hash` column so `embed_batch` can skip calling the embeddings API
entirely for chunks whose content hasn't changed since the last run — re-running the DAG twice
in a row with no source changes costs zero embedding calls, not just zero duplicate writes.

**The eval gate** runs the same `backend/evaluation/runner.py` functions `scripts/run_eval.py`
uses, but only fails the DAG on retrieval regressions (`recall@3`, `MRR`) — those are pure
vector-math over what this run just ingested, so a drop is a real signal. The generation metrics
(LLM-as-judge faithfulness/relevance) are computed and logged every run too, but they run against
a fixed adversarial fixture unrelated to today's ingested content, so gating the DAG on them would
be gating on judge noise rather than anything this run actually touched.

No Docker needed to run this locally — see [`airflow/README.md`](airflow/README.md) for the
exact setup (a plain venv + `airflow standalone`), which was used to verify the DAG parses and
executes under real Airflow 3.1 before this was written up here.

## Tests

```bash
PYTHONPATH=. python -m pytest backend/tests/ -v -c backend/pytest.ini   # 30 tests
PYTHONPATH=. mypy backend --ignore-missing-imports                      # clean
```

Tests run against a separate `aipr_review_test` database (auto-created), never the dev
database used for manual demos — the test fixtures drop all tables on teardown.
