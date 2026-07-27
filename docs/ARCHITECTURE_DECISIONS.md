# Architecture & Tech Stack Decisions

Every decision below follows the same shape: **what we chose, what we didn't, why,
and what we gave up.** That's also the shape a good interviewer will probe — see
`INTERVIEW_PREP.md` for how these get grilled.

---

## 1. Orchestration: LangGraph, not Temporal or hand-rolled asyncio

**Chosen:** LangGraph `StateGraph`, in-process, four specialist nodes fan out in
parallel from one `build_context` node and join at one `aggregate` node.

**Alternatives considered:**
- **Temporal** — a separate durable-workflow server + worker processes.
- **Hand-rolled `asyncio.gather`** — no framework, just fan the four coroutines out manually.

**Why LangGraph:**
- Zero extra infrastructure — runs inside the same Python process as the API/worker.
- First-class parallel fan-out (the Pregel-style execution model runs all nodes
  whose predecessors completed in the same superstep — the four specialists execute
  concurrently without me writing any concurrency-coordination code).
- Tight integration with LLM tool-calling and structured output, which is most of
  what this system does.

**What we gave up:**
- Temporal is battle-hardened at real scale (Uber, Netflix) with much stronger
  durability guarantees. LangGraph is newer and unproven at very high concurrent-
  workflow counts.
- Hand-rolled asyncio would have zero new dependencies at all, but reinvents
  checkpointing and retry semantics LangGraph gives for free — and gives *nothing*
  on crash recovery (a crash mid-review loses all progress).

**The seam that makes this reversible:** all orchestrator code depends on
`backend/core/workflow_engine.py` (an abstract `run/resume/get_state` interface),
never on LangGraph directly (`backend/orchestrator/langgraph_engine.py` is the only
file that imports `langgraph`). If concurrent workflow volume ever demands Temporal,
a `TemporalWorkflowEngine` implementing the same interface swaps in — one file
changes, nothing else in the codebase does. This is the single most defensible
architectural move in the project: **the expensive decision is deferred, not avoided.**

**Revisit when:** sustained concurrent reviews exceed ~50/minute, or Redis-backed
checkpointing (see #7) proves insufficient against data loss on worker crashes.

---

## 2. Database: One Tiger Cloud (Postgres + extensions) store, not three separate ones

**Chosen:** a single Postgres-compatible database (Tiger Cloud / TimescaleDB) carrying
three "lanes": vector memory (`pgvector` + `pgvectorscale`/DiskANN), time-series events
(hypertables + continuous aggregates), and relational truth (plain tables).

**Alternatives considered:**
- **Qdrant** (vectors) + **plain Postgres** (truth) + **ClickHouse** (events) — one
  purpose-built store per data shape.
- Plain Postgres only, no vector/time-series extensions.

**Why one store:**
- The product's actual questions cross all three shapes at once: *"for this PR,
  what code did we retrieve, what did we find, and what did it cost?"* Three stores
  means the app stitches that answer together in application code across three
  connection pools. One store means it's a SQL join.
- Fewer moving parts: one backup story, one connection pool, one failure mode to
  reason about, instead of three.

**What we gave up:**
- Qdrant alone is probably a better pure vector-search engine at very large scale.
- ClickHouse alone is a better pure OLAP/analytics engine for very high event volumes.
- We're betting that "good enough at each lane, in one place" beats "best-in-class
  per lane, three places" — which is only true because none of our lanes are at a
  scale where the specialized tool's edge actually matters yet.

**Revisit when:** any single lane's query patterns genuinely can't be served well by
its Postgres extension — e.g., vector search over hundreds of millions of chunks
where DiskANN's disk-resident index isn't competitive, or event volume where
continuous aggregates can't keep up with dashboard latency requirements.

---

## 3. Module structure: modular monolith, not microservices

**Chosen:** one deployable Python process (API + worker share the same codebase),
23 internal modules under `backend/`, with an enforced dependency rule: `core/`
depends on nothing, outer modules depend inward only.

**Why:** this system doesn't have independently-scaling components yet — the API,
the orchestrator, and the specialists all scale together (more PRs → more of
everything). Microservices buy you independent scaling and deployment at the cost
of network calls, service discovery, and distributed-transaction complexity — a
cost worth paying when you have the scaling mismatch that justifies it, not before.

**What keeps this reversible:** the dependency-direction invariant (checked by hand
in `.genesis/context-graph.json`) means any outer module — say, `agents/` — could
be extracted into its own service later without a rewrite, because it never
depended sideways on another outer module in the first place.

---

## 4. Web framework: FastAPI, not Flask/Django

**Why:** the whole system is IO-bound (webhook → queue → four concurrent LLM calls
→ GitHub API calls) — native `async`/`await` isn't a nice-to-have here, it's the
point. FastAPI's dependency injection also made the DB-session-per-request pattern
(`Depends(get_session)`) and Pydantic-native request/response validation trivial.
Flask's async story is bolted-on; Django's is improving but its ORM assumes sync.

---

## 5. Queue: ARQ, not Celery

**Why:** ARQ is asyncio-native — no sync/async bridge, no separate process model
to reason about, and it reuses the same Redis instance LangGraph checkpoints to
(one fewer piece of infrastructure). Celery is far more mature and has a much
larger ecosystem, but its core execution model is synchronous workers, which
would mean bridging every async LLM/DB call across a sync boundary.

**Revisit when:** you need Celery's ecosystem features (complex routing, mature
monitoring tools like Flower at scale) badly enough to pay the sync-bridge cost.

---

## 6. GitHub integration: a GitHub App, not a Personal Access Token

**Chosen:** JWT-signed App auth → short-lived installation tokens → webhook-driven.

**Alternative:** a PAT + polling for new/updated PRs.

**Why the App:** narrower permissions scoped to only the repos it's installed on
(not "everything this human can access"), higher API rate limits, and it's not
tied to a person's account (doesn't break if someone leaves). The cost is real
setup overhead — App registration, a private key, and a publicly reachable webhook
URL (ngrok for local dev) — which is exactly why M1's demo command doesn't require
any of this; it's deferred to M2 specifically so the core logic could be built and
tested for free first.

---

## 7. LLM client: a provider-agnostic interface, not a direct OpenAI/Anthropic dependency

**Chosen:** `backend/tools/llm_client.py` defines an `LLMClient` ABC with
`OpenAILLMClient`, `AnthropicLLMClient`, and — critically — a `MockLLMClient` that
returns deterministic canned findings for known trigger patterns, selected by
`LLM_PROVIDER` env var, with `mock` as the default.

**Why:** the same "hide the expensive decision behind a seam" principle as #1.
Concretely, it means:
- M1's entire test suite and demo run with **zero API keys, zero network calls,
  zero cost, zero flakiness** — the mock client is what let me actually verify
  the LangGraph fan-out and HITL gate end-to-end before any real credentials existed.
- Switching between OpenAI and Anthropic in production is a one-line env var change,
  not a code change.

---

## 8. Confidence-weighted HITL gate: minimum, not average, and why 0.75

**Chosen:** `overall_confidence = min(f.confidence for f in findings)` — the
aggregator's confidence is the *weakest* link, not the mean. Any single `CRITICAL`
finding escalates regardless of confidence; below-threshold confidence (default
0.75) routes to a human queue; otherwise the review posts automatically.

**Why minimum, not average:** averaging lets three high-confidence findings mask
one low-confidence one — exactly the "almost-right" failure mode this system is
designed against. A senior reviewer's overall trust in a review is dominated by
its shakiest claim, not the mean of all claims.

**Why 0.75 as a default, not derived:** it's a starting point, deliberately
conservative (per the "start with more human involvement than you think you need"
principle) — meant to be tuned down as the system earns trust with real dispute-rate
data from `hitl_feedback`, not treated as a fixed constant.

---

## 9. Testing strategy: mock LLM + an isolated test database

**Decision (found the hard way, mid-build):** tests must run against a dedicated
`aipr_review_test` database, auto-created in `conftest.py`, never the dev database
used for manual/live demos.

**Why this needed a fix:** the `db_session` test fixture drops all tables on
teardown for hygiene between tests. That's correct *for a test database* and
silently destructive if pointed at whatever database you're also using to demo
against. This is exactly the kind of bug that only shows up when you actually run
the thing end-to-end rather than trust that the unit tests passing means the
system works — which is why every claim of "this works" in this project is backed
by a real command and its real output, not just green test output.
