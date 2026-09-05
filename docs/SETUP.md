# Setup — getting live credentials for the M2 demo

M1 runs with zero credentials (mock LLM, local Postgres/Redis). To demo against a
real PR, you need three things. Do them in this order — GitHub first, since it's
the one you'll actually watch fire.

Everything you generate below goes into a local `.env` file (copy `.env.example`
to `.env`), never into this chat, never into git. `.env` is already in `.gitignore`.

---

## 1. GitHub App

This is what receives the webhook when a PR opens, and posts the review comment back.

1. Open **https://github.com/settings/apps/new** (log into the GitHub account/org
   that owns whichever repo you'll demo against).
2. **GitHub App name**: anything unique, e.g. `aipr-review-agent-yourname`. GitHub
   will reject it if the name is taken — just add a suffix.
3. **Homepage URL**: paste your repo's URL (e.g. `https://github.com/you/demo-repo`).
   It's just informational, doesn't need to resolve to anything special.
4. **Webhook** section:
   - GitHub needs a public HTTPS URL to POST to — your laptop on `localhost:8000`
     isn't reachable from GitHub's servers. Open a **second terminal** and run:
     ```bash
     ngrok http 8000
     ```
     (Install first if needed: `brew install ngrok`, then `ngrok config add-authtoken <token>`
     using a free account from ngrok.com.) It'll print a line like:
     ```
     Forwarding   https://a1b2-c3d4.ngrok-free.app -> http://localhost:8000
     ```
   - **Webhook URL**: `https://a1b2-c3d4.ngrok-free.app/webhook` (your actual ngrok URL + `/webhook`).
   - **Webhook secret**: generate one yourself, don't leave it blank:
     ```bash
     openssl rand -hex 32
     ```
     Copy the output. This exact string goes in two places: the GitHub App form field,
     and `.env` as `GITHUB_WEBHOOK_SECRET`.
5. Scroll to **Repository permissions**:
   - **Pull requests** → set to **Read and write**.
   - **Contents** → set to **Read-only**.
   - Leave everything else as "No access".
6. Scroll to **Subscribe to events** → check **Pull request**.
7. **Where can this GitHub App be installed?** → "Only on this account" is fine.
8. Click **Create GitHub App** at the bottom.
9. You land on the App's settings page. At the top, note the **App ID** (a number)
   → this is `GITHUB_APP_ID`.
10. Scroll down to **Private keys** → click **Generate a private key**. A `.pem`
    file downloads automatically. Move it somewhere outside the git repo entirely,
    or into a `secrets/` folder inside the repo (already gitignored):
    ```bash
    mkdir -p ~/Desktop/AIPRReviewAgent/secrets
    mv ~/Downloads/aipr-review-agent-yourname.*.private-key.pem \
       ~/Desktop/AIPRReviewAgent/secrets/github-app-private-key.pem
    ```
    That path → `GITHUB_PRIVATE_KEY_PATH`.
11. In the left sidebar of the App's page, click **Install App** → pick your
    account/org → **Only select repositories** → choose the one repo you'll demo
    against → **Install**.

You now have all four GitHub values. ngrok must stay running in its terminal for
the whole demo session — if it restarts, the URL changes and you have to update
the webhook URL in the App settings again (ngrok free tier gives a new random URL
each time you start it).

---

## 2. Tiger Cloud (the database)

1. Go to **https://console.cloud.timescale.com/signup**.
2. Fill in name, work email, a password (12+ characters) → submit → check your
   email and click the verification link.
3. Once logged in, click **Create a new service** (or similar "New Service" button).
4. Pick the free trial option — **$1,000 credit, 30 days, no credit card**.
   Choose the smallest instance size offered; this is a demo, not production load.
5. Wait ~1-2 minutes for it to provision (there's a progress indicator).
6. Open the service → find the **Connection info** / **Connection string** tab.
   You'll see something like:
   ```
   postgres://tsdbadmin:AbC123XyZ@abc123.tsdb.cloud.timescale.com:12345/tsdb?sslmode=require
   ```
7. Convert it to the asyncpg-driver form for `.env` (swap `postgres://` for
   `postgresql+asyncpg://`, and change `sslmode=require` to `ssl=require`):
   ```
   TIGER_DATABASE_URL=postgresql+asyncpg://tsdbadmin:AbC123XyZ@abc123.tsdb.cloud.timescale.com:12345/tsdb?ssl=require
   ```
8. Once this is in `.env`, run the M2 migration to add the vector/timeseries lanes:
   ```bash
   source .venv/bin/activate
   psql "$(python -c "import os; print(os.environ.get('TIGER_DATABASE_URL',''))" 2>/dev/null || true)"
   # or simpler — psql accepts the non-asyncpg form directly:
   psql "postgres://tsdbadmin:AbC123XyZ@abc123.tsdb.cloud.timescale.com:12345/tsdb?sslmode=require" \
     -f scripts/migrations/2026-06-tiger-init.sql
   PYTHONPATH=. python -m scripts.init_db   # creates the truth-lane tables too
   ```

---

## 3. LLM provider

Pick one. The code already supports both (`backend/tools/llm_client.py`) — this is
a one-line env var swap either way.

### Option A — OpenAI (what the architecture doc assumes)
1. **https://platform.openai.com/api-keys** → log in → **Create new secret key**.
2. Copy it immediately — it's shown once. → `OPENAI_API_KEY` in `.env`.
3. Set `LLM_PROVIDER=openai` in `.env`.
4. Heads up: OpenAI generally requires a payment method on file for API access
   now — check **platform.openai.com/settings/organization/billing** if the key
   gets rejected with a quota error.

### Option B — Anthropic (if you'd rather not add a card)
1. **https://console.anthropic.com/settings/keys** → **Create Key**.
2. Copy it → `ANTHROPIC_API_KEY` in `.env`.
3. Set `LLM_PROVIDER=anthropic` in `.env`.

### Option C — Ollama (free, local, no account at all)
1. `brew install ollama` (or see ollama.com for other platforms), then `ollama serve`.
2. `ollama pull qwen2.5:14b-instruct` (~9GB download; a smaller model works too, just
   less reliably — see `docs/INTERVIEW_PREP.md` for what changed between model sizes
   in testing).
3. Set `LLM_PROVIDER=ollama` in `.env`. No API key needed.

### Option D — Groq (free hosted API — what the deployed instance uses)
Local Ollama can't run on free-tier cloud hosting (not enough RAM for model
weights), so the deployed version needs a real hosted API instead.
1. **https://console.groq.com/keys** → sign up → **Create API Key**.
2. Copy it → `GROQ_API_KEY` in `.env`.
3. Set `LLM_PROVIDER=groq` in `.env`. Free tier includes `openai/gpt-oss-120b`
   — a bigger model than the 14B run locally in testing. (Groq deprecated the
   original `llama-3.3-70b-versatile` this project used on 2026-08-16 — this
   has already bitten the deployed instance once in production, see
   `docs/INTERVIEW_PREP.md`'s bugs-found section.)

---

## 4. LangSmith tracing (optional)

Traces the LangGraph run itself — per-node latency, token usage, errors across the
four-specialist fan-out. Complements `agent_events` (the business-level audit/cost
ledger) rather than replacing it. Free tier, no card required.

1. **https://smith.langchain.com** → sign up → **Settings** → **API Keys** →
   **Create API Key**. Key type: Personal Access Token. Expiration: `Never` is
   simplest for a portfolio project, or `90d` if you'd rather rotate periodically.
2. Add to `.env`:
   ```
   LANGSMITH_TRACING=true
   LANGSMITH_API_KEY=<your key>
   LANGSMITH_PROJECT=aipr-review-agent
   ```
3. Run any review (locally or via the deployed webhook) and check
   **smith.langchain.com** → your project → you'll see one trace per review,
   with the four specialists' nodes nested under it as parallel children.

Heads up: PR diff content flows through LangSmith's servers as part of the trace
when this is on — the same untrusted-input consideration as any other LLM call
in this system, just routed through a third party now too.

---

## Running the live demo

```bash
cd ~/Desktop/AIPRReviewAgent
source .venv/bin/activate
cp .env.example .env   # then fill in the values above

docker compose up -d
PYTHONPATH=. python -m scripts.init_db

# terminal 2
ngrok http 8000

# terminal 3
PYTHONPATH=. uvicorn backend.api.main:app --reload --port 8000

# terminal 4
PYTHONPATH=. arq backend.job_queue.arq_worker.WorkerSettings
```

Open a pull request on the repo you installed the App on. Within a few seconds
a review comment should appear, and:
```bash
curl localhost:8000/api/reviews/<review_id_from_the_worker_log>
```
should return the persisted findings.

---

## Running Agent Eval

Retrieval and generation metrics for the RAG pipeline — separate from the app
itself, this is an offline harness you run by hand, output goes straight to
your terminal (no dashboard yet, see `docs/INTERVIEW_PREP.md`'s "deferred
until scale" table for why not).

```bash
source .venv/bin/activate
PYTHONPATH=. python -m scripts.ingest_docs   # only needed once, or after a DB reset
PYTHONPATH=. python -m scripts.run_eval
```

Real output from this repo's Tiger Cloud instance + local Ollama embeddings:

```
=== Retrieval metrics (Recall@3, MRR) ===
  recall@3=1.00  top1=workflow_engine_seam                      'Can backend/agents/ import directly from backend/job_qu'
  recall@3=1.00  top1=reliability_layer_required                'Is it OK to call httpx directly with no timeout in a ne'
  recall@3=1.00  top1=workflow_engine_seam                      'Should orchestrator code import langgraph directly, or '
  recall@3=1.00  top1=llm_client_seam                           'Is it fine for a new module to call the OpenAI SDK dire'
  recall@3=1.00  top1=hitl_confidence_gate                      'Should the aggregator average confidence across finding'
  recall@3=1.00  top1=untrusted_content_boundary                'Is it safe to interpolate PR diff content directly into'
  MRR: 0.889

=== Generation metrics (LLM-as-judge: Faithfulness, Relevance) ===
  [grounded_finding] supported=True (OK)  relevant=True (OK)
  [ungrounded_finding_no_context] supported=False (OK)  relevant=True (OK)
  [hallucinated_finding] supported=False (OK)  relevant=False (OK)

  Faithfulness rate: 0.33
  Relevance rate:    0.67
```

Reading this: Recall@3 is 1.00 on every query — the right chunk always lands
in the top 3 — but MRR (0.889, not 1.000) shows it isn't always rank 1, which
is the more sensitive signal since `context_retriever.py` only ever uses
`top_k=5` chunks, not just the top 1. The generation numbers aren't meant to
look good — `GENERATION_TEST_SET` (`backend/evaluation/dataset.py`) is a
deliberately mixed fixture (one grounded finding, one ungrounded, one
hallucinated), so 0.33 faithfulness / 0.67 relevance is the harness correctly
scoring a bad test set as bad, not a real quality number for the live
pipeline. Run it against real findings pulled from `finding_records` to get
that instead — not wired up yet, see the gap table below.

Unit tests for the parts of this that don't need a live DB (the pure metric
functions, and the LLM-as-judge functions against `MockLLMClient`) run as
part of the normal suite:
```bash
PYTHONPATH=. python -m pytest backend/tests/test_evaluation_metrics.py -v
```

---

## Deploying for free (so anyone can try it, no laptop required)

This runs the whole pipeline on Render's free tier, using Groq (Option D above)
for the LLM instead of local Ollama — free hosting doesn't have the RAM to run
local model weights, so the deployed instance needs a real hosted API. RAG
retrieval (the architecture-grounding capability) is wired for the deployed
instance too: Groq has no embeddings API, so `EMBEDDING_PROVIDER=gemini`
(`gemini-embedding-001`, truncated to 768 dims via the `output_dimensionality`
param in `embedder.py`) covers just the embedding call — Groq still handles
all LLM reasoning. Gemini was picked over OpenAI specifically because its
free tier needs no payment method at all (OpenAI's embeddings API requires a
funded account, even though the actual usage cost here is a few cents).
Local runs with Ollama still get full grounding through its own embeddings
endpoint; all three providers are configured independently of each other.

**Four new free accounts needed: Render (hosting), Groq (LLM), Google AI
Studio (`aistudio.google.com/apikey` — embeddings, genuinely free, no card),
on top of the GitHub App and Tiger Cloud you already set up above.**

Two operational steps that are easy to miss when switching the deployed
instance's `EMBEDDING_PROVIDER`: (1) `GEMINI_API_KEY` has to be added as a
secret in Render's environment variable UI (`render.yaml` marks it
`sync: false`, so Render won't set a value for you); (2) the vectors already
sitting in `code_chunks` on Tiger Cloud were computed with whatever provider
ingested them — switching providers without re-running
`PYTHONPATH=. python -m scripts.ingest_docs` (with `EMBEDDING_PROVIDER=gemini`
set locally, pointed at the same `TIGER_DATABASE_URL`) leaves query-time
vectors and stored vectors in different embedding spaces, which silently
degrades retrieval rather than erroring.

### 1. Render account + service

1. **https://dashboard.render.com/register** → sign up (GitHub OAuth is easiest —
   it'll also let you grant repo access in the same step). No credit card should
   be asked for on the free tier.
2. Once logged in, click **New +** → **Blueprint**.
3. Connect the `ai-pr-review-agent` GitHub repo (grant Render access if prompted).
   Render will detect `render.yaml` in the repo root and show you two services:
   `aipr-review-agent` (the web service) and `aipr-review-queue` (free Redis).
4. Click **Apply** / **Create New Resources**. Render will prompt you to fill in
   the env vars marked `sync: false` in `render.yaml` — paste in:
   - `TIGER_DATABASE_URL` — the same value from your local `.env`
   - `GROQ_API_KEY` — from step 3 below
   - `GITHUB_APP_ID`, `GITHUB_WEBHOOK_SECRET` — same values as your local `.env`
   - `GITHUB_PRIVATE_KEY` — **not** the file path this time. Open your `.pem`
     file in a text editor, copy its *entire contents* (including the
     `-----BEGIN...` / `-----END...` lines), and paste the whole thing as the
     value.
   - `LANGSMITH_API_KEY` — optional, same value as your local `.env` if you set
     up tracing (section 4 above); leave blank to skip it on the deployed instance.
5. Click deploy. First build takes a few minutes. Once live, Render shows a URL
   like `https://aipr-review-agent.onrender.com`.

### 2. Groq API key

1. **https://console.groq.com/keys** → sign up → **Create API Key** → copy it.
   That's the `GROQ_API_KEY` value from step 1.4 above.

### 3. Point the GitHub App at the deployed URL

1. Go back to your GitHub App's settings page (**github.com/settings/apps** →
   your app → General).
2. **Webhook URL** → replace the old ngrok URL with:
   `https://aipr-review-agent.onrender.com/webhook` (use your actual Render URL).
3. Save.

### 4. Try it

Open a pull request on the repo (or have someone else fork it and open one
against your repo — the App fires on any PR opened against the repo it's
installed on, regardless of who opens it). Render's free tier sleeps after 15
minutes of inactivity, so the very first request after a quiet period can take
30-60 seconds to wake up — GitHub will retry the webhook delivery automatically
if the first attempt times out, so the review still lands, just not instantly
on a cold start.

```bash
curl https://aipr-review-agent.onrender.com/health
curl https://aipr-review-agent.onrender.com/api/reviews/<review_id>
```
