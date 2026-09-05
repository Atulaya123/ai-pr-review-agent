# Running the knowledge-base-refresh DAG locally

`airflow/dags/knowledge_base_refresh.py` — verified against real Airflow 3.1.0
(`DagBag` parses it with zero import errors; task order and the retry/backoff
config on `embed_batch` were checked directly, not just eyeballed). No Docker
required — `airflow standalone` runs the scheduler, webserver, and triggerer
in one process, which is all a 6-task sequential DAG needs.

Airflow 3.x specifically, not 2.x: Airflow 2.10's own dependency constraints
pin SQLAlchemy 1.4, which conflicts with this repo's SQLAlchemy 2.0 usage
(`backend/database/models.py` uses `DeclarativeBase`, a 2.0-only API).
Airflow 3.x moved its own floor to SQLAlchemy 2.0, which is what actually
resolves the conflict — this isn't a preference, 2.x will fail to import
this project's code at all.

## 1. One-time setup

```bash
# a separate venv from the project's own .venv — Airflow pins a lot of its
# own dependency versions, better kept isolated from backend/requirements.txt
python3.12 -m venv airflow/.venv
source airflow/.venv/bin/activate

AIRFLOW_VERSION=3.1.0
PYTHON_VERSION=3.12
pip install "apache-airflow==${AIRFLOW_VERSION}" \
  --constraint "https://raw.githubusercontent.com/apache/airflow/constraints-${AIRFLOW_VERSION}/constraints-${PYTHON_VERSION}.txt"

pip install -r airflow/requirements.txt
```

## 2. Point Airflow at this repo's DAG folder and run it

Run from the **repo root** (not `airflow/`) — `backend/core/config.py` loads
`.env` relative to the current directory, and the DAG imports `backend.*` /
`scripts.*` directly, so both need the repo root on `PYTHONPATH` and as `cwd`.

```bash
source airflow/.venv/bin/activate
export AIRFLOW_HOME="$(pwd)/airflow/.airflow_home"
export AIRFLOW__CORE__DAGS_FOLDER="$(pwd)/airflow/dags"
export AIRFLOW__CORE__LOAD_EXAMPLES=False
export PYTHONPATH="$(pwd)"

airflow standalone
```

First run prints an admin password to the terminal (also saved to
`airflow/.airflow_home/simple_auth_manager_passwords.json.generated`) — open
**http://localhost:8080**, log in as `admin` with that password.

## 3. What you need before triggering a real run

The DAG calls the same `backend.memory.embedder` / `backend.memory.tiger_client`
code the rest of the app uses, so it needs the same things `scripts/ingest_docs.py`
already needs:

- `TIGER_DATABASE_URL` in `.env` pointing at a database with
  `scripts/migrations/2026-06-tiger-init.sql` **and**
  `scripts/migrations/2026-09-content-hash.sql` already applied (the second
  one adds `content_hash`, which `embed_batch` needs to decide what to skip).
- An embedding provider that's actually reachable: local Ollama running with
  `nomic-embed-text` pulled (the default), or `EMBEDDING_PROVIDER=gemini` with
  a free `GEMINI_API_KEY` from `aistudio.google.com/apikey` (no card needed —
  what the deployed instance uses), or `EMBEDDING_PROVIDER=openai` with a
  funded `OPENAI_API_KEY`.
- If Tiger Cloud's IP allowlist is enabled, this machine's current IP needs to
  be on it (`docs/SETUP.md` — this has bitten this exact project before, when
  a run failed with a connection timeout rather than an auth error).

## 4. Trigger and watch it

```bash
airflow dags trigger knowledge_base_refresh
```

Or click the DAG in the UI and hit the trigger (▶) button. Click into a run to
see the graph view — each task's log is one click away, which is the actual
point of using Airflow over a cron job calling `ingest_docs.py` directly:
when something fails, you see *which* task failed and why, not just that the
9pm run didn't produce output.

To confirm the idempotency design actually works: trigger the DAG twice in a
row without changing any of the six invariants in `scripts/ingest_docs.py`.
The second run's `embed_batch` log should read
`0 embedded, 6 unchanged/skipped` — no embedding-API calls made, because
every chunk's content hash still matches what's already in `code_chunks`.

## 5. Forcing the gate to fail (to see what that looks like)

Lower `RECALL_AT_3_THRESHOLD` in the DAG file to something below what your
retrieval actually scores, or temporarily point `TIGER_DATABASE_URL` at an
empty database (nothing ingested yet, so every query returns nothing and
recall is 0) — `gate` should raise `AirflowFailException`, the task turns red
in the UI, and `run_eval`'s output is still visible in its own log even
though the DAG run as a whole is marked failed.
