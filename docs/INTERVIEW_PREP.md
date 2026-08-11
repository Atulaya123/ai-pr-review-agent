# Interview Prep — defending this project

Organized so you can skim by category. Each answer is deliberately short — say the
first two sentences, then let them pull the thread if they want depth. The real
debugging stories at the bottom are your strongest material — they prove you built
and ran this, not just prompted an agent to generate files.

---

## System design

**Q: Walk me through what happens when someone opens a PR.**
GitHub sends a `pull_request` webhook. FastAPI verifies its HMAC signature and
checks an idempotency key (the delivery ID) before doing anything else, then
enqueues a job to Redis/ARQ and returns 200 immediately — the heavy work happens
async in a worker so a slow LLM call can never make the webhook endpoint itself
time out. The worker runs a LangGraph orchestrator: one node builds retrieval
context, then four specialist agents (security/quality/tests/docs) run in
parallel, each grounded in that context, each returning structured `Finding`
objects. An aggregator dedups overlapping findings, computes an overall
confidence, and applies a confidence-weighted gate: auto-post if confident and
nothing critical, otherwise route to a human approval queue. Every step writes
an event row for tracing/cost/audit.

**Q: Why four separate agents instead of one prompt?**
A single prompt collapses four genuinely different mindsets — "could this be
exploited," "is the logic right," "what's untested," "will this be understood" —
into one shallow pass, and it hallucinates with no way to audit which concern
produced which claim. Splitting them means each agent does one job deeply, and a
finding's provenance (`agent_type`) is part of the data model, not an afterthought.

**Q: Why is this a modular monolith and not microservices?**
Nothing in this system scales independently yet — more PRs means more of
everything, uniformly. Microservices buy independent scaling and deployment at
the cost of network calls and distributed complexity; that's a cost worth paying
when you actually have the scaling mismatch that justifies it. I enforced an
inward-only dependency rule instead, so any module could be extracted into its
own service later without a rewrite, if and when that mismatch shows up.

**Q: How would this scale to 10,000 PRs/minute?**
Three specific bottlenecks, in the order they'd hit: the single ARQ worker process
(horizontally scale worker replicas — the queue already decouples this), the
in-process LangGraph checkpointer (swap `MemorySaver` for the Redis-backed one,
already planned as a drop-in via the `WorkflowEngine` interface), and GitHub API
rate limits (the App's installation-scoped limits are per-installation, so this
mostly parallelizes naturally across repos).

---

## AI / LLM specific

**Q: How do you prevent hallucination?**
Two layers: grounding (each specialist gets retrieved codebase context, not just
the bare diff — an LLM judging a diff in isolation doesn't know what a function
overrides or what convention it violates, so it guesses confidently) and a
confidence field on every finding that drives the HITL gate, so low-confidence
claims never reach a human as if they were certain.

**Q: How do you debug a multi-agent system when something's wrong with one
specialist's output but not the others?** LangSmith, added after the fact —
it traces the LangGraph run as a tree, the four specialists as parallel
children of the review, each expandable to the exact prompt and response.
Before it was wired up, diagnosing the grounding-not-being-used bug (see
Field Notes below) meant manually calling the retrieval function in isolation
to confirm it worked, then separately inspecting raw LLM outputs to notice
they never cited the retrieved rules — two disconnected checks. With LangSmith
in place now, that same class of bug is one trace to look at instead of two
separate manual verifications, which is the actual reason I added it rather
than treating the custom `agent_events` table as sufficient on its own.

**Q: How do you handle prompt injection from a malicious diff?**
Two independent defenses, not one. Structural: diff/code content is always fenced
with explicit `<<<UNTRUSTED_...>>>` delimiters and the system prompt explicitly
instructs the model to treat that block as data, never instructions. Heuristic: a
small pattern-matcher (`backend/security/injection_guard.py`) flags known injection
phrasings ("ignore previous instructions," etc.) so an attempt is at least visible,
even though regex-matching alone is not a complete defense — the structural fencing
is the real control; the heuristic is a tripwire on top of it.

**Q: Why min() and not average for overall confidence?**
Averaging lets three high-confidence findings mask one low-confidence one — the
"almost-right" failure mode where a review looks 90% trustworthy but is subtly
wrong in the 10% that matters. A senior reviewer's trust in a review is set by its
shakiest claim, not the mean of all claims, so the gate uses the minimum.

**Q: Why not just fine-tune a model instead of prompting + retrieval?**
Fine-tuning bakes in a snapshot of "how to review" that goes stale as the codebase
and team conventions evolve, and it's expensive to iterate on. RAG over the actual,
current codebase means the grounding is always fresh, and swapping/upgrading the
underlying model (the whole point of the provider-agnostic `LLMClient` abstraction)
doesn't require retraining anything.

**Q: How do you verify retrieval is actually *grounding* the model, not just
decorating the prompt?** Two separate checks, because they can fail
independently. First, I called the retrieval function directly with a known
diff and printed what came back — confirmed the right chunks were fetched and
ranked correctly by similarity, with zero LLM involved. Second, and separately,
I checked whether the model's *actual answers* cited that content. The first
one passed immediately; the second one didn't at first — the prompt only
mentioned retrieved context in passing, so the model had the facts but wasn't
instructed to actively cross-check the diff against them. Wiring the pipe and
getting the model to use it turned out to be two different engineering
problems, and conflating them would have hidden a real gap.

**Q: Why did you need a bigger model instead of just better prompts?**
I tried both independently. A 3B local model without a "check against these
rules" instruction hallucinated (invented a nonexistent SQL injection in code
with no SQL at all). A 7B model with the improved prompt got close — flagged
the right function, gestured at the right area — but never named the actual
rule. Only moving to 14B, with the same improved prompt, reliably produced the
precise catch, citing the project's own invariant by name across three
independent specialists. My conclusion: prompt engineering fixes instruction-
following, but there's a reasoning-depth floor below which no amount of
prompting reliably gets you a precise, correctly-attributed judgment call —
you have to identify which failures are "the model isn't told what to do" vs.
"the model isn't capable enough," because the fix is different for each.

**Q: What would you say to "just use GPT-4/Claude for everything, why bother
with local models"?** For a real production system, I'd agree — hosted
frontier models would likely be faster and more consistent than a local 14B
model, and I'd default to one if cost weren't a constraint. But the constraint
was real (no budget for API credits for this project), and the exercise proved
something worth knowing either way: the system's provider-agnostic `LLMClient`
interface means swapping to a hosted model later is a one-line config change,
not a redesign — the architecture was built assuming the model would need to
change, which is exactly what happened during development.

---

## Data / database

**Q: Why one Postgres-compatible database instead of Qdrant + Postgres + ClickHouse?**
Because the product's real questions — "what code did we retrieve for this PR,
what did we find, what did it cost" — cross all three data shapes (vector, truth,
time) at once. Three stores means stitching that answer together across three
connection pools in application code; one store means it's a SQL join. The
trade-off is giving up best-in-class performance per lane in exchange for far
less operational surface area — a bet that only holds while none of the lanes
are at a scale where the specialized tool's edge actually matters.

**Q: What's a hypertable and why do you need one?**
It's TimescaleDB's partitioning of a normal-looking table by time range under the
hood — `agent_events` has one row per span/LLM-call/tool-call, and queries are
almost always "recent" (last hour, last day), so partitioning by time means those
queries touch a narrow slice instead of the whole history. Continuous aggregates
sit on top precomputing rollups (cost per minute, p95 latency) so the dashboard
never scans raw events on every page load.

**Q: Why SQLAlchemy (an ORM) instead of raw asyncpg?**
Productivity for the truth-lane relational tables (reviews, findings, HITL rows,
foreign keys) — the ORM's relationship mapping and migrations are worth it there.
The plan (per the architecture doc's own trade-off table) is to bypass the ORM for
genuinely hot paths like raw event inserts, using asyncpg directly where per-row
overhead actually matters.

---

## Reliability / production readiness

**Q: What happens when the LLM provider times out?**
Three layers, each doing a different job: a bounded timeout on every call (so
nothing hangs forever), retry with exponential backoff + jitter for transient
failures, and a circuit breaker that opens after repeated failures so a dead
provider fails fast instead of piling up more timeouts — the LangGraph graph
itself has per-node isolation so one stuck specialist can't deadlock the others.

**Q: How do you prevent posting the same review twice on a webhook retry?**
Idempotency at ingress: GitHub's `X-GitHub-Delivery` header is a unique ID per
delivery attempt, inserted into a `webhook_deliveries` table with `ON CONFLICT DO
NOTHING`; a duplicate delivery gets acknowledged with 200 but never re-enqueued.

**Q: What's your rollback story if a bad model update starts producing garbage findings?**
The `LLM_PROVIDER`/model fields are config, not code — reverting is an env var
change and a redeploy, not a rollback of application logic. Longer-term (M4), a
golden-dataset regression gate in CI would catch a quality regression before it
ships at all.

**Q: What happens if a step after the "important" work fails — e.g., posting to
GitHub succeeds but something afterward throws?** I hit this for real: a
downstream Slack-notification call (no timeout, no error handling — a
deliberately-planted flaw for a demo) threw an unhandled exception *after* the
review had already posted to GitHub but *before* the result was saved to our
own database. ARQ's automatic retry then re-ran the whole job, which happened
to produce a different, non-crashing outcome on the retry — so the database
ended up silently missing a review that GitHub definitely has. That's a real
distributed-systems problem (a multi-step operation with side effects in two
different systems has no atomicity between them) with a known fix I'd apply
next: persist the review result immediately after posting, before attempting
any secondary/best-effort side effect like a Slack ping, and wrap the
secondary effect in a try/except so it literally cannot affect the primary
outcome, matching the reliability-layer pattern the rest of the codebase
already uses everywhere else.

---

## Trade-offs (the "why X not Y" gauntlet)

Keep these to two sentences each: what you chose, what you gave up. Full detail
in `ARCHITECTURE_DECISIONS.md` if they want to go deeper on any one.

| Decision | Chose | Over | Because | Gave up |
|---|---|---|---|---|
| Orchestration | LangGraph | Temporal | Zero extra infra, first-class fan-out | Temporal's durability at real scale |
| Database | One Tiger Cloud store | Qdrant+Postgres+ClickHouse | Cross-lane queries are one join, not three | Best-in-class per lane |
| Structure | Modular monolith | Microservices | No scaling mismatch yet | Independent scaling/deploy |
| Queue | ARQ | Celery | Asyncio-native, no sync bridge | Celery's mature ecosystem |
| GitHub auth | GitHub App | Personal Access Token | Scoped permissions, higher rate limits | Setup simplicity |
| Confidence | min() | average | Doesn't let good findings mask a bad one | Slightly more conservative gate |

**Q: Is this deployed live, and what did that require?**
Yes — https://aipr-review-agent.onrender.com, on Render's free tier, fully
public: fork the repo, open a PR, and the pipeline runs for real. Free hosting
doesn't have the RAM to run the local 14B model that produced the strongest
local results, so the deployed instance swaps to Groq's free hosted API
(`llama-3.3-70b-versatile`) — a one-line config change behind the same
`LLMClient` interface, which is exactly the point of that abstraction. Two
real constraints shaped the deployment: Render's free tier has no background-
worker service type at all, so the API and the ARQ worker run as two
supervised processes in one container instead of two separate services; and
Groq has no embeddings API, so the deployed instance's RAG grounding falls
back to diff-only reasoning — local Ollama runs are the only ones with full
architecture-grounding. `LLM_PROVIDER` stays fully configurable either way.

---

## Deferred until scale actually demands it

Every row here was a conscious "not yet," not an oversight — the third column is the
actual trigger that would justify switching, not a vague "eventually we should."

| Today | At real scale | What would force the switch |
|---|---|---|
| Slack notification called inline, isolated with try/except | Transactional outbox: write the review row + an outbox event in one Postgres transaction, a separate relay publishes to a broker (Kafka/SQS), Slack — and any future consumer — subscribes independently | More than one downstream consumer of "a review was posted." Right now there's exactly one (Slack), so decoupling it buys nothing yet — outbox+Kafka solves fan-out to *many* consumers, not the dual-write bug I actually hit, which the persist-then-isolate fix already closes |
| Redis/ARQ as the job queue | Kafka, SQS, or another durable broker | Redis has no durability guarantee by default — a crash without persistence configured loses in-flight jobs, and there's no replay. Fine while losing a job means re-running a demo; a real risk once losing a job means losing a real review |
| In-memory LangGraph checkpointer (`MemorySaver`) | Redis- or Postgres-backed checkpointer | A worker crash mid-review currently loses that run's progress silently — acceptable for a demo, not once a review is slow/expensive enough that re-running from scratch is a real cost |
| Single ARQ worker process | Horizontally scaled worker pool | Queue depth growing faster than one worker drains it. The queue already decouples this, so scaling out is a deploy/config change, not a redesign |
| Offline Agent Eval (`scripts/run_eval.py`), run by hand against a fixed test set | Production monitoring: sample 1-5% of live traffic, async evaluation off the request path, dashboards, threshold alerts, `hitl_feedback` disputes feeding back into the test set | A measured drift problem — the offline harness can't tell you retrieval quality degraded as the corpus grew, only production sampling over time can. `hitl_feedback` already captures the human-dispute signal that flywheel would consume; nothing reads it yet |

**Say this if asked "why not just build the scalable version now":** "Every one of these
has a known, standard fix, and I can name it — the reason I didn't build it yet is that
none of the triggers that would justify it exist at this project's actual scale. Building
for a load pattern that doesn't exist is its own cost: more infra to run, monitor, and
explain, for a problem I don't have. So each one is scoped as 'here's the fix, here's
what would tell me it's time,' not skipped and not built prematurely either."

---

## Where this maps onto standard GenAI system-design patterns

Cross-checked against three reference frameworks (FinOps cost-optimization, MCP,
Agentic RAG) to see what's actually built versus what's a plausible next step.
Verified against the code, not the architecture diagram — the `daily_budget_usd`
row below is exactly the kind of gap that only shows up this way.

**Implemented:**

| Pattern | Where | Note |
|---|---|---|
| RAG (single-source vector retrieval) | `backend/memory/` (`embedder.py`, `context_retriever.py`, `tiger_client.py`) | Top-k cosine similarity over `pgvector`/DiskANN, no relevance grading — see gap below |
| LLM Gateway | `backend/tools/llm_client.py` | Provider-agnostic `LLMClient` ABC — Mock/OpenAI/Anthropic/Groq/Ollama behind one interface |
| Multi-agent collaboration | `backend/agents/` + `backend/orchestrator/` | Four specialist reasoners (security, quality, tests, docs) fan out in parallel, one aggregator merges — this *is* the "Multi-Agent Collaboration" agentic pattern |
| Stateful workflow / state management | `backend/orchestrator/graph.py`, `state.py` | LangGraph `StateGraph`, Pregel-style superstep execution, checkpointing |
| Response validation gate | `backend/orchestrator/nodes.py` (aggregator, `min(confidence)`) | Confidence-weighted HITL escalation — validates the specialists' self-reported confidence, **not** groundedness against retrieved sources (that's a real gap, see below) |
| Resilience: retry, circuit breaking, timeouts | `backend/reliability/` (`retry.py`, `circuit_breaker.py`, `timeout.py`) | Exponential backoff + jitter, per-dependency circuit breaker, provider-specific timeout ceilings (90s for local Ollama vs 20s default) |
| Graceful degradation | `context_retriever.py` | Retrieval failure is caught and swallowed — falls back to diff-only reasoning rather than sinking the review |
| Structured output | `llm_client.py` (`response_format={"type": "json_object"}`) | JSON mode, not schema-constrained function calling |
| Cost/audit observability | `agent_events` hypertable + optional LangSmith tracing | Per-action cost ledger (business-level) plus opt-in execution-level tracing — see decision #10 in `ARCHITECTURE_DECISIONS.md` |
| Agent Eval: retrieval metrics | `backend/evaluation/retrieval_metrics.py` (pure) + `scripts/run_eval.py` (DB-backed) | Recall@k and MRR against a hand-built query→expected-chunk test set (`backend/evaluation/dataset.py`) — run via `python -m scripts.run_eval` after `python -m scripts.ingest_docs` |
| Agent Eval: generation metrics | `backend/evaluation/generation_metrics.py` | Faithfulness (does a finding's rationale trace back to retrieved context?) and Relevance (does it actually address the diff?) via LLM-as-judge — this is the real fix for the groundedness gap the confidence gate doesn't cover, see below |

**Gaps — real, not oversights, each with why it'd matter:**

| Pattern | Status | Why it'd matter |
|---|---|---|
| Semantic response caching | Not implemented — only `functools.lru_cache` on static prompt templates | Genuine cost lever: similar diffs across PRs in the same repo are a natural cache key; industry numbers put semantic-cache hit rates at 15-70% |
| Tiered / complexity-based model routing | Not implemented — one model per environment via `LLM_PROVIDER`, not per-request | Cheap-model-first routing for simple diffs, escalate only when needed, is a standard 30-50% cost lever this doesn't use yet |
| Document relevance grading (Corrective RAG) | Not implemented — top-k chunks are used unconditionally | No check that retrieved context is actually relevant before handing it to specialists; a bad embedding match currently degrades silently. Different from Agent Eval's faithfulness check above: that scores output *after* generation, this would filter input *before* it |
| Real chunking strategy | Not implemented — `scripts/ingest_docs.py` hardcodes six hand-written paragraphs, one per architectural invariant, embedded whole | See the chunking-strategies note below — the `code_chunks` schema (`path`, `symbol`, `chunk_index`) already anticipates document-aware chunking, ingestion just doesn't do it yet |
| Query reformulation, multi-source routing | Not implemented — single vector-DB source, no fallback to web/SQL/parametric | Only matters if retrieval quality becomes a measured problem; premature before that |
| Reflection / iterative self-correction | Not implemented — single-pass generation per specialist | No agent re-evaluates its own output before it's aggregated |
| MCP | Not implemented | Natural next step — fits behind the existing `LLMClient`/`workflow_engine` seams without restructuring anything |
| Budget enforcement | **Looks implemented, isn't** — `daily_budget_usd` exists in `backend/core/config.py` but nothing in the codebase reads it | A config field with no enforcement path is worse than no field: it reads as a control that doesn't actually control anything |
| Fine-tuning (LoRA/QLoRA) | Not applicable | No self-hosted model — every provider is a hosted API, so there's nothing to fine-tune onto |

**Say this if asked "what's Agent Eval scoring, exactly, and why isn't the confidence
gate enough":** "The HITL gate (`backend/hitl/gate.py`) takes the minimum confidence a
specialist *reports about itself* — it catches a specialist that says 'I'm only 40% sure,'
but it can't catch a specialist that's fully confident and wrong. Faithfulness scoring
closes that gap: it's a second LLM call that checks the finding's rationale against the
actual retrieved context, the same way a human reviewer asks 'where does it say that?'
instead of trusting the tone of the answer. They're complementary, not redundant — one's
about the specialist's own uncertainty, the other's about whether the claim is actually
true given what it saw."

**Chunking strategies — where this project actually stands:** there isn't a real chunking
pipeline here. `scripts/ingest_docs.py` embeds six manually written paragraphs verbatim —
each one pre-sized to be a single coherent unit, no splitting logic at all. That's fine for
"ingest this project's own six invariants," but it wouldn't survive contact with "ingest
this repo's actual source files." The three standard strategies, and which one this
project's schema is already shaped for:
- **Fixed-size** (e.g. 512 tokens, 10-20% overlap): simplest, zero semantic awareness,
  best for logs/transcripts — not a good fit here, the content is source code and docs.
- **Semantic** (embed sentences, split at similarity drops): 15-40% precision improvement
  on structured documents, but adds ingestion-time compute — worth it once retrieval
  quality is actually measured and found wanting (see Corrective RAG gap above).
- **Document-aware** (Markdown → headers, source code → function/class boundaries): the
  best fit for this codebase, and notably the `code_chunks` table already has `path`,
  `symbol`, and `chunk_index` columns — the schema was built anticipating function/class-
  level chunks, ingestion just hasn't caught up to what the schema already supports.

**RAG evaluation framework — what's built vs. the full production picture:** Agent Eval
(above) covers the test-set-driven half of a real RAG eval framework: Recall@k/MRR for
retrieval, Faithfulness/Relevance via LLM-as-judge for generation, run offline against a
fixed test set. What it deliberately doesn't do yet is the *production monitoring* half —
sampling 1-5% of live traffic, running evaluation async off the request path, dashboards,
threshold-based alerts, and feeding disputed findings back into the test set as a data
flywheel. `hitl_feedback` (`backend/database/models.py`) already captures human dispute
signal per finding — the raw material for that flywheel exists, nothing reads it yet. That
production-monitoring layer is a "Deferred until scale actually demands it" item, same
reasoning as the rest of that table: it's real infrastructure to build and run, worth it
once there's actual production traffic and a measured drift problem, not before.

**Say this if asked "why not add semantic caching / model routing / MCP now":**
"Same answer as the infra-scaling table above — I can name the standard fix for
each of these, and none of them are free: caching adds a staleness-vs-hit-rate
tuning problem, tiered routing needs a complexity classifier that itself costs
something to get wrong, MCP is worth it once there's a second consumer of these
tools beyond this one pipeline. Building them now would be optimizing a cost
structure I haven't measured yet."

---

## "Tell me about a bug you ran into" — use the real ones

These are strictly better answers than anything hypothetical, because they
actually happened while building this:

1. **Event-loop mismatch under test.** A cached SQLAlchemy async engine held
   pooled connections tied to one event loop, but pytest-asyncio created a new
   loop per test by default → `RuntimeError: got Future attached to a different
   loop`. Fixed by scoping the event loop to the whole test session. *What this
   shows: understanding of asyncio's execution model, not just "tests pass."*

2. **A destructive test fixture pointed at the wrong database.** The test
   teardown dropped all tables for hygiene — correct for a test DB, silently
   catastrophic pointed at the same database used for a live manual demo. Caught
   it by actually running the live demo after the test suite, not by trusting
   green CI. Fixed with an auto-created, isolated `aipr_review_test` database.
   *What this shows: verifying real behavior, not just trusting test output.*

3. **Async lazy-loading crash on a relationship.** `GET /api/reviews/{id}`
   500'd with `MissingGreenlet` because SQLAlchemy's default lazy-load on
   `PRReviewRecord.findings` tried to do IO outside an awaitable context after
   the session had already moved on. Fixed with `lazy="selectin"` for eager
   loading. *What this shows: know the sharp edges of async ORMs, not just how
   to write a model.*

4. **Port collisions with existing local services.** Docker's Postgres/Redis
   silently connected to the wrong service — a native Postgres and an SSH tunnel
   already had 5432/6379 (and 5433) bound. Diagnosed with `lsof`, not by guessing;
   remapped to unused ports and updated every config in lockstep. *What this
   shows: methodical debugging under an ambiguous symptom ("role postgres does
   not exist" on a container that clearly had that role).*

5. **A hallucinated finding at 100% self-reported confidence.** Testing the
   RAG-grounded review, the docs specialist claimed a function "lacks a
   docstring" — it has one, clearly, in the diff — and reported that claim at
   confidence 1.00, the maximum. Meanwhile correct findings in the same review
   sat at 0.85-0.95. This is the core argument for why the aggregator's
   confidence gate uses the *minimum* across findings rather than trusting any
   single score at face value: self-reported LLM confidence is generated text,
   not a measured probability, and a wrong finding can carry higher confidence
   than a correct one. *What this shows: understanding that LLM confidence
   scores need independent verification, not blind trust, even when the number
   looks reassuring.*

6. **A downstream failure silently erased evidence of a successful primary
   action.** Covered in detail in Reliability above — an unhandled exception in
   a deliberately-flawed Slack notification crashed a job *after* GitHub had
   already received a posted review, and the automatic retry masked it by
   re-running until a different outcome avoided the crash. GitHub had a review;
   the database didn't know about it. *What this shows: side effects across
   multiple external systems need ordering and isolation, not just individual
   error handling — retries can hide a consistency bug instead of just fixing
   a transient one.*

7. **A dimension mismatch and a unique-constraint collision, both in the same
   migration.** Wiring real vector search: the schema was written for 256-dim
   embeddings (matching OpenAI's model per the architecture doc), but the free
   local embedding model produces 768-dim vectors — a straightforward `ALTER
   COLUMN ... TYPE vector(768)` plus rebuilding the index fixed it. Separately,
   an ingestion script inserting multiple chunks under the same file path
   hit a unique constraint on `(repo, path, chunk_index)` because every row
   was hardcoded to `chunk_index=0` — fixed by actually enumerating the loop.
   *What this shows: schema assumptions written for one provider don't
   silently carry over to another, and "should be unique" needs to actually be
   computed, not assumed.*

8. **Free-tier hosting quietly rules out a whole architecture choice.** Render's
   free tier only offers one process type (a web service) — no free background-
   worker service at all. The fix wasn't code, it was re-scoping the deployment:
   run the API and the ARQ worker as two processes inside one container, with a
   supervisor script that exits (triggering Render's restart) if either process
   dies, so a silent worker crash can't leave half the pipeline running
   invisibly. *What this shows: infrastructure constraints on a target platform
   can force an architecture decision as much as a technical requirement can —
   worth checking a platform's actual free-tier limits before assuming a design
   will just deploy as-is.*

9. **An IP allowlist blocks a host that has no IP to allow.** The database
   connected fine locally (after allowlisting my own IP earlier) but Render's
   deploy hung with a `TimeoutError` — the exact same signature as the original
   local IP-block symptom. Render's free tier doesn't provide a static outbound
   IP (that's a paid add-on), so there was no single IP to add. Fix: remove the
   IP allowlist entirely for this database, accepting that access control now
   rests solely on the password. *What this shows: a security control that
   depends on a stable network identity breaks for serverless/free-tier
   compute by design, not by misconfiguration — the honest trade-off is between
   IP-based restriction and paying for a static IP, not a fix that gets you both
   for free.*

10. **A dashboard silently corrupted a multi-line secret.** Pasting the GitHub
    App's private key (a standard multi-line PEM) into Render's environment
    variable field produced `InvalidKeyError: Could not parse the provided
    public key` — the paste had been mangled somewhere between clipboard and
    stored value. Rather than trust the next paste to go cleanly, I made the
    parsing defensive: strip surrounding quotes, normalize `\r\n`, and unescape
    literal `\n` sequences, so the code tolerates the paste even if the
    dashboard mangles it a similar way again. *What this shows: when a bug
    boundary is a third-party UI you don't control, the fix belongs on your
    side of that boundary — you can't patch their paste handling, only your
    parsing.*

11. **A test that cleaned up after itself still leaked a real network call.**
    Adding LangSmith tracing, I wrote a test that set `LANGSMITH_TRACING=true`
    with a fake key, asserted the env vars exported correctly, then reverted
    them in a `finally` block. The test suite still logged a real (harmless,
    403) HTTP call to `api.smith.langchain.com` — after the "passed" summary
    line, not during. LangChain's tracing client buffers and flushes on a
    background thread at process exit, independent of whatever the env var
    said by the time that thread ran; reverting the var promptly wasn't good
    enough because the client object had already been initialized with the
    fake key. Fix: extract the env-var computation into a pure function with
    no side effects, and test *that* — never actually flip the real tracing
    switch in a test at all. *What this shows: "I cleaned up after the test"
    isn't the same guarantee as "the test had no side effects" — a library
    can hold onto state (or a background thread) longer than your test scope
    assumes, and the fix is to avoid triggering the real mechanism, not to
    clean up faster.*
