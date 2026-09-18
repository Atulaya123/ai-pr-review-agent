# "The project you can defend" — nine-angle drill

Answers to the HelpMeSwitch "project you can defend" module, applied to this actual
project (the AI PR Review Agent), not a hypothetical support bot. Say the answer out
loud in ~90 seconds before reading it here. Each one names a number, an alternative
that was rejected, and a real failure — those three are what the follow-up ladder in
the module is hunting for. Deeper material for almost everything below already lives
in `docs/ARCHITECTURE_DECISIONS.md` (decision-by-decision trade-offs) and
`docs/INTERVIEW_PREP.md` (Q&A + the 14-bug field-notes list) — this doc is the
90-second version, tuned to these nine specific questions, with pointers back to the
long version for follow-ups.

---

## Q2.1 — Walk me through the most substantial thing you've built with an LLM.

I built an AI PR review agent: four specialist LLM reasoners — security, quality,
tests, docs — fan out in parallel over a GitHub pull request diff and post a review,
with a confidence-weighted gate that routes to a human instead of posting when
nothing's confident enough.

One real request, start to finish: GitHub sends a `pull_request` webhook → FastAPI
verifies the HMAC signature and checks `X-GitHub-Delivery` against a
`webhook_deliveries` table (`ON CONFLICT DO NOTHING`) so a retried delivery is
acknowledged but never re-enqueued → job goes on Redis/ARQ, 200 returns immediately
→ an ARQ worker picks it up and runs a LangGraph orchestrator: one `build_context`
node does hybrid retrieval (pgvector/DiskANN cosine similarity fused with Postgres
full-text search via Reciprocal Rank Fusion), then four specialist nodes run concurrently in
the same LangGraph superstep, each grounded in that retrieved context → an aggregator
dedups overlapping findings and takes `min()` of every finding's confidence, not the
average → below 0.93 it posts nothing and instead calls
`GitHubClient.request_human_review()` (a PR comment plus a `needs-human-review`
label), otherwise it posts the review directly.

What I owned versus what the framework gave me: LangGraph supplies the parallel
fan-out and superstep execution model — I never wrote concurrency-coordination code
for the four specialists. I wrote the hybrid retrieval fusion, the aggregator's
dedup + min-confidence logic, the HITL escalation call path (the schema
`enqueue_hitl_review()` existed early on but nothing called it — I wired that), the
Redis checkpointer (hand-rolled against `BaseCheckpointSaver`, not the official
package — see Q2.9), and the provider-agnostic `LLMClient` interface.

**Numbers worth quoting:** 39 tests pass end-to-end against a mock LLM client with
zero network calls; live-tested against 5 real GitHub PRs (#9–#13) against the
deployed instance; 12 real findings across 4 of those PRs, every one landing in the
0.90–0.99 confidence band; Recall@3 = 1.00, MRR 0.889 → 1.000 across retrieval
strategies.

**Real failure to have ready:** the docs specialist once flagged a function as
"lacks a docstring" — it had one, visibly, in the diff — and reported that claim at
confidence **1.00**, the maximum, while correct findings in the same review sat at
0.85–0.95. That single incident is the actual argument for `min()` over average
(Q2.3) — a wrong finding can outscore a correct one, so no single self-reported
number can be trusted at face value.

---

## Q2.2 — Why use an LLM here at all? What did you consider and rule out first?

The system is deliberately not "LLM for everything." Before any diff reaches a
model, two cheap non-LLM gates run first: HMAC + idempotency verification at
ingress (plain code, rejects malformed/duplicate webhooks before they cost anything),
and a regex-based injection heuristic (`backend/security/injection_guard.py`) that
flags known prompt-injection phrasings as a tripwire. Neither of those needed a
model.

The part that genuinely needed an LLM is judgment that a linter can't produce:
"could this be exploited," "is the logic right," "what's untested," "will this be
understood" are four different lenses over the same diff, and none of them reduce
to pattern matching against a ruleset — that's exactly why it's four specialist
prompts instead of one, and why RAG grounding exists at all: a model judging a bare
diff doesn't know what convention it violates or what function it overrides, so it
guesses confidently instead of checking.

**What it cost that a linter wouldn't:** real latency (bounded by per-call timeouts,
retry with backoff, and a circuit breaker so a dead provider fails fast rather than
piling up hangs) and a real dollar cost per call. That second one is a genuine gap
worth naming honestly, not glossing over: `daily_budget_usd` exists as a config
field but nothing in the codebase currently enforces it — a control that doesn't
control anything yet.

**Kept the cheap path on purpose:** the injection heuristic and the idempotency
check both run before any specialist is invoked, so a duplicate or malformed
webhook never reaches an LLM call at all. And the confidence *threshold* itself
(0.93) is a config constant, not something the model decides — the boundary between
"post automatically" and "ask a human" is enforced in plain code, deliberately kept
out of the model's hands.

---

## Q2.3 — Which parts were the model's, and which were yours?

The model's job, strictly: given a diff plus retrieved context, return structured
`Finding` objects (severity, confidence, rationale) via JSON mode. That's it —
retrieval, aggregation, and the escalation decision are all mine.

**The swap-one-step test:** `LLM_PROVIDER` is an env var behind an `LLMClient` ABC
(`OpenAILLMClient`, `AnthropicLLMClient`, a `MockLLMClient` for tests, Groq, Ollama).
Switching providers in production is a one-line config change — this is exactly
what happened mid-build, moving from a local Ollama model to Groq's hosted API for
deployment, and again when Groq silently deprecated the exact model this project
depended on (`llama-3.3-70b-versatile`, cut off 2026-08-16) and the fix was one line
in `_LLM_MODEL_BY_PROVIDER`, not a redesign.

**Model-right, system-wrong case:** the docstring hallucination from Q2.1. The model
followed the output format correctly and reported high confidence; the system was
wrong to trust that number at face value, which is exactly the gap the
aggregator's `min()` gate exists to close — it catches a specialist that says "I'm
only 40% sure," but nothing at the confidence-gate layer catches a specialist that's
*fully* confident and wrong. That second gap is what the offline faithfulness/
relevance LLM-as-judge eval (Q2.5) is for instead — a genuinely separate check, run
offline, not on the hot path.

**A concrete guardrail I wrote just to stop the model doing something:** the
injection tripwire, and the structural fencing (diff content wrapped in
`<<<UNTRUSTED_...>>>` delimiters with an explicit system-prompt instruction to treat
it as data, never instructions) — two independent defenses against the diff itself
trying to steer the model, not one.

**Logic I kept in code on purpose, though the model could plausibly have done it:**
the confidence threshold (0.93) is a constant, not a model decision. Idempotency and
HMAC verification never touch the model at all. And escalation routing —
auto-post vs. `needs-human-review` — is a plain `if` on a computed number, not
something asked of the model.

---

## Q2.4 — What did the first version get wrong once real people actually used it?

Two real incidents, not a sanitized "tuned the prompts" story.

**The HITL gate was structurally unreachable.** The original
`HITL_CONFIDENCE_THRESHOLD` default was 0.75, picked as a conservative starting
guess and never validated against the deployed model's actual behavior. I found
this by deliberately live-testing it, not by luck: two PRs (#11, #12) written to be
progressively more ambiguous, designed specifically to invite a hedge, both still
came back `REQUEST_CHANGES` at 90%+ confidence. Across all 4 live PRs, every one of
12 findings landed in 0.90–0.99 — nothing this model reports ever comes close to
0.75, so escalation could never trigger, silently. I recalibrated to 0.93 (strictly
above the one 0.90 outlier actually observed, strictly below the typical
0.95–0.99 band) and re-ran #11's exact scenario as a new PR (#13) after deploying
the fix: `needs-human-review` label applied, a comment reading "overall confidence
0.70 is below this reviewer's threshold" posted instead of a formal review. Detected
by design, confirmed live, not assumed from the math alone.

**A downstream failure erased evidence of a successful primary action.** A
Slack-notification call (deliberately left without a timeout or error handling)
threw *after* a review had already posted to GitHub but *before* the result was
saved to the app's own database. ARQ's automatic retry re-ran the whole job, which
happened to succeed differently on the retry — so GitHub had a posted review the
database never recorded. Blast radius: a real consistency gap between two systems
with no shared transaction, the kind of bug that only shows up running the thing
end-to-end, not from green tests. Fix I'd apply (not yet shipped): persist the
review result immediately after posting, before any secondary/best-effort side
effect, and isolate that side effect in a try/except so it structurally cannot
affect the primary outcome — the same reliability pattern already used everywhere
else in the codebase.

**What exists now that didn't in version one:** a calibrated, live-confirmed
threshold instead of a guessed one, and a named, scoped fix for the dual-write gap
even though that fix isn't implemented yet — see Q2.7.

---

## Q2.5 — How did you actually know it was working? What did you measure?

Two separate evaluation tracks, both real, both re-run rather than quoted once.

**Retrieval (Recall@3, MRR):** a hand-built query → expected-chunk test set
(`backend/evaluation/dataset.py`), scored by `backend/evaluation/retrieval_metrics.py`
against the real Tiger Cloud instance. Recall@3 has been **1.00** on every
provider/retrieval-strategy combination tried. MRR went from **0.889** (Ollama, pure
vector) to **1.000** (Gemini, pure vector) — and stayed at 1.000, unchanged, after
adding the hybrid RRF fusion lane. That last number is the honest one: a change I
expected to help (hybrid fusion) showed zero measurable improvement on this test
set, and I reported that instead of assuming it helped because it sounded like it
should.

**Generation (Faithfulness, Relevance):** LLM-as-judge via
`backend/evaluation/generation_metrics.py` against a deliberately-mixed adversarial
test set — does a finding's rationale actually trace back to retrieved context, does
it address the diff. Both land around **0.33**, with real run-to-run variance since
the judge is itself a live LLM call, not a fixed lookup. That number needs one more
sentence or it reads as "wrong two-thirds of the time": 0.33 is roughly what this
fixture is *designed* to produce — it deliberately contains one grounded, one
ungrounded, and one hallucinated finding, so a correctly-behaving judge should score
about a third. It measures whether the harness correctly separates good findings
from bad ones, not the live system's faithfulness rate. I don't have a production
faithfulness number, because online eval isn't wired up yet — that's the same
production-monitoring gap named in Q2.8.

**Real-system verification on top of the offline eval:** the HITL calibration from
Q2.4 — 4 live PRs, 12 findings, all logged — was itself a measurement exercise, not
a vibe check; the 0.75 → 0.93 change came directly from that data, and PR #13
confirmed the math held against the real deployed system, not just on paper.

**What's still done by hand, not automated:** production-traffic monitoring.
Everything above runs offline against a fixed test set, by running
`python -m scripts.run_eval` manually — not sampled continuously off live traffic.
`hitl_feedback` (a table for human disputes on findings) already captures the raw
signal a real feedback flywheel would need; nothing reads it yet. That's a named,
deferred gap, not an oversight — see Q2.8.

---

## Q2.6 — What did it actually cost to run, and how did you find that out?

The honest answer starts with a real gap: `daily_budget_usd` exists as a config
field in `backend/core/config.py`, and nothing in the codebase reads it. That's
worse than not having the field at all — it reads as a control that doesn't
actually control anything — and it's the first thing I'd fix before trusting this
unattended at any real volume (Q2.8).

What I do have is per-action logging: `agent_events`, a TimescaleDB hypertable, has
one row per action/LLM-call/tool-call — the schema for a real cost ledger exists,
it's just not rolled up into an enforced dollar limit yet.

**Where the actual cost decisions live is in provider choice, not runtime
enforcement.** Everything is deployed on free tiers specifically to keep this at
zero marginal cost: Render's free web-service tier (no free background-worker type
at all, so the API and ARQ worker run as two supervised processes in one
container), Groq's free hosted API for LLM reasoning, and Gemini's free embeddings
tier for RAG grounding specifically because it needs no payment method — OpenAI's
embeddings path is fully implemented and works (`EMBEDDING_PROVIDER=openai`), it's
just not what's deployed, since that one path requires a funded account for what
would otherwise be a few cents of usage.

**A real quality-for-compute trade I made during development:** testing local
models before switching to a hosted API, a 3B model without a "check against these
rules" instruction hallucinated a SQL injection in code with no SQL at all; a 7B
model with the improved prompt got close — flagged the right function, gestured at
the right area — but never named the actual rule; only 14B reliably produced the
precise, correctly-attributed catch. That's the model-size floor below which no
amount of prompt engineering closes the gap — a real cost/quality trade, not a free
win, and I'd default to a hosted frontier model over any of this if budget weren't
a constraint.

**The spike I didn't expect wasn't a dollar spike, it was an availability one:**
Groq silently deprecated the exact model this project depended on
(`llama-3.3-70b-versatile`, cut off 2026-08-16). I found out by coincidence, opening
an unrelated test PR, not by monitoring — every specialist's call started failing
with `model_not_found`, visible in Render's logs. Fixed with a one-line model-string
change. Worth naming directly: `daily_budget_usd`-style cost tracking wouldn't have
caught this even if it were wired up — a failing call doesn't get expensive, it just
fails — and the offline eval harness defaults to `LLM_PROVIDER=mock`, so it would
have stayed green through the whole outage. It took a live PR to surface it.

---

## Q2.7 — If you were rebuilding this today, what would you do differently?

The one decision I'd reverse first: skip the six-hand-written-paragraph ingestion
shortcut and build real document-aware chunking from the start.

`scripts/ingest_docs.py` currently embeds six manually written paragraphs verbatim,
one per architectural invariant, no splitting logic at all. It got me to a working
RAG demo fast, which was the right call for proving the retrieval pipeline worked —
but it doesn't survive contact with "ingest this repo's actual source files," which
is the obvious next step for this system to be useful beyond its own six rules. I
only noticed the gap was structural, not incidental, when I looked at what the
`code_chunks` schema already has: `path`, `symbol`, `chunk_index` columns — the
schema was built anticipating function/class-level chunks from day one; ingestion
just never caught up to what it already supports.

What I'd build instead: a document-aware chunker — Markdown split at headers,
source code split at function/class boundaries — which is the standard strategy
that actually fits this content (fixed-size chunking is the wrong tool for source
code; semantic chunking adds ingestion-time compute that isn't justified until
retrieval quality is a measured problem, which it currently isn't — Recall@3 is
already 1.00 on the toy corpus).

Was this a copied tutorial default or my own decision? Mine — there was no tutorial
here, it was a deliberate scope cut to get the RAG pipeline verifiable end-to-end
before spending time on ingestion sophistication that a 6-paragraph corpus doesn't
need yet.

Rebuilding it today would cost roughly a chunking-strategy implementation pass
plus a re-ingestion run against the live Tiger DB (embeddings would need to be
regenerated either way, same operational care as any embedding-provider switch) —
worth doing specifically because it's the blocker between "reviews itself" and
"reviews any repo it's installed on."

---

## Q2.8 — This was a side project. What would have to change to run it at real scale?

This maps directly onto a table I already keep honest in
`docs/INTERVIEW_PREP.md` — every row is a conscious "not yet," with a named trigger,
not an oversight:

**What only works because volume is low right now:** the HITL calibration in Q2.4
was validated against 4 live PRs run by hand, and the offline eval harness is run
manually via `python -m scripts.run_eval`. Neither survives real traffic — the
"deferred until scale demands it" fix is production monitoring: sample 1–5% of live
traffic, run evaluation async off the request path, dashboards, threshold alerts,
and feed `hitl_feedback` disputes back into the test set as a real data flywheel.
The raw signal (`hitl_feedback`) is already captured in the schema; nothing reads
it yet.

**What would break first:** the queue. Redis/ARQ has no durability guarantee by
default — a crash without persistence configured loses in-flight jobs with no
replay. Fine when losing a job means re-running a demo PR; a real risk once losing
a job means losing a real review someone was waiting on. The trigger to switch to
Kafka/SQS is exactly that risk becoming real, not a vague "eventually."

**What would get meaningfully more expensive:** LLM calls, and — see Q2.6 —
there's currently no enforced budget ceiling on them at all. Before trusting this
unattended at volume I'd wire real enforcement on `daily_budget_usd` and add rate
limiting, since right now nothing stops a burst of traffic from running the bill up
silently the way the Groq deprecation ran up *failures* silently.

**Concurrency:** a single ARQ worker process today. The queue already decouples
this — scaling out worker replicas is a deploy/config change, not a redesign — and
GitHub App rate limits are installation-scoped, so they mostly parallelize
naturally across repos. What I haven't verified is Postgres/pgvector connection
pool behavior under many concurrent worker replicas hitting the same DB at once.

**Rollback path:** currently, reverting a bad model is an env var change and a
redeploy — genuinely fine. What's missing is catching a *quality* regression before
it ships at all — a golden-dataset regression gate in CI, planned as M4 but not
built. Right now a regression is caught by me noticing, which is exactly the kind
of manual safety net that doesn't survive past a handful of users, let alone real
scale.

---

## Q2.9 — Drill: five decisions, not features, with a rejected alternative and a number each

1. **Chose `min()` over average** for the aggregator's overall confidence, after a
   hallucinated finding scored **1.00** confidence in the same review where correct
   findings sat at 0.85–0.95 — averaging would have let that one wrong claim hide
   inside three good ones.

2. **Rejected the official `langgraph-checkpoint-redis` package** for a hand-rolled
   `BaseCheckpointSaver` against the plain `redis` client, after hitting a real
   `ModuleNotFoundError`: its `redisvl` dependency needs `redis>=6.3`, and `arq`
   (already in the stack as the job queue) hard-pins `redis<6` — no single version
   satisfies both, verified by actually installing both, not by reading a changelog.

3. **Chose LangGraph over hand-rolled `asyncio.gather`**, accepting a real
   durability/maturity gap versus Temporal at very high concurrent-workflow counts,
   in exchange for zero extra infrastructure and free Pregel-style parallel
   fan-out — but scoped the bet to be reversible: all orchestrator code depends on
   one `WorkflowEngine` abstract interface, so swapping in Temporal later touches
   one file, not a rewrite.

4. **Rejected the untested default `HITL_CONFIDENCE_THRESHOLD` of 0.75** after live
   PRs showed 12/12 real findings landing at 0.90–0.99, making escalation
   structurally unreachable — recalibrated to **0.93** and confirmed live on a 5th
   PR that it actually escalates, not just computes correctly on paper.

5. **Chose Gemini over OpenAI for deployed embeddings**, rejecting the more common
   paid path, specifically because Gemini's free tier needs no card — accepted the
   real operational cost that switching either LLM or embedding provider requires
   re-running ingestion so stored and query-time vectors share one embedding space,
   a mismatch that degrades retrieval silently instead of erroring loudly.

**Which bullet collapses first under two follow-ups, honestly:** none of the five
above — the weakest claim in this whole project is the one named directly in Q2.6
and Q2.8: `daily_budget_usd` is a config field with no enforcement path behind it. If
pushed on "so cost is actually controlled how," the honest answer is "it isn't yet,
here's exactly what's missing and why I know it's missing" — not a bullet that
pretends otherwise.
