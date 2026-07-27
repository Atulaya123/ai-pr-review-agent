# AI PR Review Agent

A production-grade AI pull-request review agent: four specialist reasoners (security,
quality, tests, docs) fan out over a diff in parallel via LangGraph, each grounded in
retrieved codebase context, merged by a confidence-weighted HITL aggregator, with every
action written to an events spine. Full design rationale in [`pr-review-agent.html`](pr-review-agent.html).

Built with the [genesis-kit](https://github.com/ayush488-glitch/genesis-kit) ritual — see
[`.genesis/`](.genesis/) for the cognitive design, invariants, and milestone plan this was
built against.

**Docs:** [`docs/SETUP.md`](docs/SETUP.md) (step-by-step credential setup for the live demo)
· [`docs/ARCHITECTURE_DECISIONS.md`](docs/ARCHITECTURE_DECISIONS.md) (tech stack & design
trade-offs, with what was given up) · [`docs/INTERVIEW_PREP.md`](docs/INTERVIEW_PREP.md)
(Q&A defense of this project, including real bugs found while building it)

## Status

**M1 — Core review loop, local & mocked — done.** Ingress → queue → LangGraph fan-out
(security/quality/tests/docs) → aggregator → confidence-weighted HITL gate → persistence,
all running locally with a mock LLM client (no API keys required). See `.genesis/PLAN.md`
for M2 (live GitHub App + Tiger Cloud + OpenAI), M3 (dashboard/economics), M4 (reliability/
security hardening).

## Architecture at a glance

```
GitHub PR → FastAPI ingress (HMAC + idempotency) → Redis/ARQ queue → ARQ worker
  → LangGraph orchestrator ─┬─ security agent  ─┐
                            ├─ quality agent   ─┤→ aggregator (dedup, confidence, HITL gate) → GitHub review
                            ├─ tests agent     ─┤
                            └─ docs agent      ─┘
  → agent_events (observability) + pr_review_records/finding_records (truth) in Postgres/Tiger Cloud
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

All 14 tests should pass, including `test_pipeline_flags_sql_injection_and_blocks` — the
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

## Getting to a live demo (M2)

You need three sets of credentials — full step-by-step for each is in the conversation
history / ask again if needed:

1. **GitHub App** (webhook + posting reviews) → `GITHUB_APP_ID`, `GITHUB_WEBHOOK_SECRET`,
   `GITHUB_PRIVATE_KEY_PATH`
2. **Tiger Cloud** (the database — free trial, no card) → `TIGER_DATABASE_URL`, then run
   `scripts/migrations/2026-06-tiger-init.sql` against it
3. **OpenAI** (or Anthropic) → `OPENAI_API_KEY` and set `LLM_PROVIDER=openai`

Put all of these in `.env` (gitignored) — never in source, never committed.

## Project layout

See `.genesis/DONE.html` section 4 and `pr-review-agent.html` section 4.2 for the full
module map. Everything under `backend/` is implemented per that map; `backend/memory/`
(RAG retrieval) is stubbed pending M2 Tiger Cloud credentials.

## Tests

```bash
PYTHONPATH=. python -m pytest backend/tests/ -v -c backend/pytest.ini   # 14 tests
PYTHONPATH=. mypy backend --ignore-missing-imports                      # clean
```

Tests run against a separate `aipr_review_test` database (auto-created), never the dev
database used for manual demos — the test fixtures drop all tables on teardown.
