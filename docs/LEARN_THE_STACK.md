# Learn the Stack

You didn't type this code line by line, so this doc exists to close that gap before anyone
asks you about it. Read it in order — each section builds on the last. For each tool: what
it is, why *this project specifically* needs it, and a one-liner you can actually say out
loud if someone asks "so what does X do here?"

Skip nothing on your first pass, even sections that sound familiar — the "why this project
needs it" part is where interview answers actually come from, not the generic definition.

---

## 1. The one-paragraph version of the whole system

A developer opens a pull request. GitHub sends a notification (a "webhook") to a small web
server this project runs. That server doesn't do the actual reviewing itself — it just
quickly writes "please review this" to a queue and replies OK. A separate worker process
picks that up, runs four independent AI reasoning passes over the diff in parallel (one
each for security, code quality, tests, and docs), each grounded in retrieved facts about
the codebase's own rules, merges their findings, decides whether it's confident enough to
post automatically or needs a human to look first, and — if confident — posts a real review
comment back to the PR on GitHub. Every version of "why does it need X" below traces back
to some piece of that sentence.

**Say this if asked "what does your project do" in one breath:** "It's an AI code reviewer
that runs four specialist reasoning passes over a pull request in parallel, grounds them in
the codebase's own documented conventions instead of just the diff, and only auto-posts when
its own confidence is high enough — otherwise it routes to a human."

---

## 2. Web servers, APIs, and webhooks — FastAPI

**What it is:** a Python framework for building web servers that respond to HTTP requests
(the same protocol your browser uses to load pages). "API" here just means "a URL a program
can call," as opposed to a URL a human browses.

**A webhook**, specifically: instead of your program repeatedly asking GitHub "anything new?"
(polling — wasteful), you give GitHub a URL and say "call me the instant something happens."
GitHub does the calling; your server just has to be listening.

**Why this project needs it:** `backend/api/main.py` is the FastAPI app. It has one route
that matters most, `POST /webhook` (`backend/webhook_receiver/router.py`) — the URL GitHub
calls the moment a PR opens or updates.

**HMAC signature verification** (`backend/security/webhook_verify.py`): anyone on the internet
could send a fake POST to that URL claiming to be GitHub. HMAC is a way to prove a message
really came from someone who shares a secret with you — GitHub signs every webhook with a
secret you both know (`GITHUB_WEBHOOK_SECRET`), and the server recomputes that signature and
rejects the request if it doesn't match. This is the same trust problem as a wax seal on a
letter, done with math instead of wax.

**Idempotency:** networks are unreliable, so GitHub sometimes delivers the same webhook twice.
"Idempotent" means "doing it twice has the same effect as doing it once." A unique delivery
ID from GitHub gets recorded the first time; a repeat is recognized and ignored.

**Say this if asked:** "The webhook receiver verifies GitHub's HMAC signature before trusting
anything in the request, and tracks delivery IDs so a retried webhook doesn't trigger a
duplicate review."

---

## 3. Why you can't just do the work inside the web request — queues, Redis, ARQ

A web request is supposed to get a fast reply (milliseconds). Reviewing a PR with four AI
calls takes tens of seconds to minutes. If the webhook handler tried to do that work directly,
GitHub's webhook delivery would time out and mark it as failed.

**The fix — a queue:** the web server's only job is to write "here's a job to do" onto a list
(the queue) and immediately reply "got it." A completely separate process (**the worker**)
continuously checks that list and does the actual slow work whenever something appears. This
decouples "acknowledge fast" from "do slow work," which is the standard pattern for anything
triggered by an external system with a timeout.

**Redis:** an in-memory database — like a regular database, but everything lives in RAM
instead of on disk, so it's extremely fast to read/write. It's overkill as a general database
but exactly right as the "list of pending jobs" storage, because that list needs to be fast
and doesn't need to survive forever.

**ARQ:** a small Python library that implements "worker checks Redis for jobs and runs them"
so you don't have to write that loop yourself. `backend/job_queue/arq_worker.py` is the code
that runs when a job comes off the queue — it's a completely separate running process from
the web server (see `docs/SETUP.md`'s "terminal 3" for the worker, a different process from
"terminal 2," the API).

**Say this if asked:** "The webhook handler enqueues to Redis and returns immediately; a
separate ARQ worker process does the actual review asynchronously, so a slow LLM call can
never make GitHub's webhook delivery time out."

---

## 4. Orchestrating multiple AI calls at once — LangGraph

**The problem it solves:** this system doesn't run one AI call, it runs four (security,
quality, tests, docs), and they should all run *at the same time* (not one after another,
which would be four times slower), then their results need to be merged by a fifth step
(the aggregator) that has to wait until all four are done.

**LangGraph:** a library for describing that kind of workflow as a graph — boxes (called
"nodes," each one a function) connected by arrows describing what runs after what. You
describe the shape once; LangGraph's engine figures out that the four specialist nodes can
run in parallel because none of them depend on each other, and that the aggregator node
should wait until all four finish before it runs. This is `backend/orchestrator/graph.py`.

**"State":** as the graph runs, there's one shared bundle of data threading through it — the
PR's diff, then each specialist's findings get added to it as they finish, then the final
merged result. `backend/orchestrator/state.py` defines exactly what's in that bundle.

**Checkpointing:** LangGraph can save its progress partway through a run, so if the process
crashes mid-review, it can resume from the last completed step instead of starting over. This
project currently uses an in-memory version of that (fine for a demo, lost on restart) — a
documented, deliberate corner cut, not an oversight (see `docs/ARCHITECTURE_DECISIONS.md` #1).

**Say this if asked:** "LangGraph runs the four specialist agents as parallel nodes in a
graph and only runs the aggregator once all four have returned — I didn't have to write any
concurrency-coordination code by hand for that."

---

## 5. Watching what the graph actually did — LangSmith

**The problem it solves:** once four things are running in parallel and merging into one
result, "why did this review come out the way it did" stops being answerable just by reading
code — you need to see the actual execution: which node ran when, how long each took, what
each specialist's LLM call was actually asked and actually answered, and where an error (if
any) happened.

**LangSmith:** a hosted tracing service, built by the same company as LangGraph, that
LangGraph can report to automatically — turn it on with three environment variables
(`AIPR_LANGSMITH_TRACING=true`, `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT`) and no code changes, and
every graph run shows up as a tree in a web dashboard: the whole review as the root, the four
specialists as parallel children, each one expandable down to the exact prompt sent and
response received. This project's `backend/core/config.py` exports those variables into the
real process environment on startup (see section 2's note on "environment variable," this is
the same mechanism) because LangGraph reads them from there, not from this project's own
settings object.

**Why this is a second, different thing from `agent_events`:** `agent_events` (the Tiger Cloud
table from section 8) is the *business*-level record — one row per review-relevant decision,
kept forever, queried by the app itself for cost/audit purposes. LangSmith is *execution*-level
tracing — every LLM call's raw prompt/response, kept for debugging, viewed by a human in a
dashboard, not queried by the application. Same underlying events, two different audiences and
two different lifetimes.

**Say this if asked:** "LangSmith traces the LangGraph execution itself — I can open a
dashboard and see the four specialists' actual prompts and responses side by side for any
review, which is what I used to debug why grounding wasn't changing the model's answers during
development, before I could fix the prompts to actually use it."

---

## 6. Talking to an LLM — prompts, JSON mode, and why models make things up

**System prompt vs. user prompt:** every LLM call has two main parts. The **system prompt**
sets the model's role and rules ("you are the security specialist, look for X, respond in
this JSON shape"). The **user prompt** is the actual thing to look at (here: the diff, plus
any retrieved context). Same model, wildly different behavior depending on the system prompt
— that's why `backend/prompts/templates/*.txt` exist as separate files, one per specialist.

**Structured/JSON output:** by default an LLM just writes prose. "JSON mode" is a setting that
constrains it to produce valid, parseable JSON matching a shape you specify, so the code that
receives the response can reliably pull out fields like `severity` and `confidence` instead
of parsing free text.

**Hallucination:** LLMs generate the statistically plausible next words, not looked-up facts.
Ask one to review code it's never seen any context about, and it will confidently invent
plausible-sounding but wrong claims. You saw this directly in this project — a model claiming
a function "lacks a docstring" when it clearly has one. This isn't a bug you patch out; it's
an inherent property of how these models work, which is *why* the system doesn't blindly
trust a single model's output (see confidence gating, section 8).

**Confidence:** models can be asked to also output "how sure am I" as a number. Important
caveat you should be ready to explain: that number is just more text the model generated — it
is not a measured, calibrated probability. This project saw a wrong finding reported at
confidence 1.00 (maximum) right alongside correct ones — self-reported confidence and actual
correctness are not the same thing.

**Say this if asked:** "Structured JSON output makes the model's response parseable by code.
Confidence is self-reported by the model, not independently verified, which is a real
limitation — I saw a hallucinated finding reported at 100% confidence in testing."

---

## 7. Grounding the model in facts it doesn't have — RAG, embeddings, vector search

**The problem:** handed only a diff, a model has no idea whether this change violates a rule
your team wrote down somewhere else in the repo. It's judging in a vacuum.

**RAG (Retrieval-Augmented Generation):** before asking the model to reason, first *fetch*
relevant facts from a knowledge store and hand those to the model alongside the question.
"Retrieval" (fetch relevant facts) + "Augmented Generation" (the model's answer is now
informed by those facts). This project's knowledge store is its own architecture rules —
`scripts/ingest_docs.py` loads real excerpts from `context-graph.json`'s invariants into the
database.

**Embeddings:** a way to turn a piece of text into a long list of numbers (a "vector") such
that texts with similar *meaning* end up as similar lists of numbers — even if they don't
share any of the same words. This project uses `nomic-embed-text` running locally via Ollama
(free) to do this; `backend/memory/embedder.py` is the code that calls it.

**Vector similarity search:** given a new piece of text (here, the diff), embed it the same
way, then ask the database "which stored chunks have the most similar number-list to this
one?" That's `backend/memory/tiger_client.py`'s `query_similar_chunks` — it uses the `<=>`
operator, which computes "cosine distance" (a standard way of measuring how similar two
vectors' directions are) between the diff's embedding and every stored chunk's embedding, and
returns the closest ones.

**pgvector / pgvectorscale / DiskANN:** `pgvector` is a Postgres extension that adds "store a
vector in a column" and "search by similarity" as native database features. `pgvectorscale`
and its `DiskANN` index make that search fast even with millions of stored vectors, by
building a smart index structure instead of comparing against every single row.

**Say this if asked:** "Retrieval works by embedding both the diff and the project's own
documented rules into vectors, then doing a cosine-similarity search in Postgres via pgvector
to find the most relevant rules before the LLM ever sees the diff. I verified retrieval was
firing correctly on its own before trusting the full pipeline's output."

---

## 8. The database — Postgres, SQLAlchemy, Tiger Cloud, TimescaleDB, hypertables

**PostgreSQL ("Postgres"):** a relational database — data stored in tables with rows and
columns, related to each other (a review has many findings; that's a "relationship").
Extremely standard, extremely reliable, and — as this project demonstrates — extensible with
add-on features (vector search, time-series) rather than needing a totally separate database
per feature.

**SQLAlchemy (async):** rather than writing raw SQL query strings everywhere in Python code,
SQLAlchemy lets you define Python classes (`backend/database/models.py`) that represent
tables, and write Python code that gets translated into SQL. "Async" means these database
calls don't block the whole program while waiting for a response — other work can happen
concurrently, which matters because this whole app is built around doing several things at
once (the four parallel specialists, for instance).

**Tiger Cloud:** a hosted (cloud) Postgres service — someone else runs and maintains the
actual database server; you just connect to it over the internet. It comes with TimescaleDB
and pgvector already installed as extensions, which is why it was chosen over plain hosted
Postgres (see `docs/ARCHITECTURE_DECISIONS.md` #2 for the full trade-off).

**TimescaleDB / hypertables:** an extension that makes Postgres better at storing
time-ordered data (this project's `agent_events` table — one row per action the system took,
timestamped). A **hypertable** looks like a normal table to your code, but internally splits
its data into time-based chunks, so a query for "events from the last hour" only has to touch
recent chunks instead of scanning the entire history.

**Continuous aggregates:** pre-computed summary tables (e.g., "cost per minute") that
TimescaleDB keeps automatically updated, so a dashboard reading "what did we spend today"
doesn't have to re-scan every raw event on every page load. (Defined in this project's
migration but not yet wired to anything — an M3 gap, honestly noted.)

**Say this if asked:** "Postgres with the pgvector and TimescaleDB extensions carries three
different data shapes — vector embeddings, time-ordered events, and normal relational rows —
in one database, which was a deliberate trade-off against running three separate specialized
databases."

---

## 9. The decision layer — confidence-weighted HITL gating

**HITL = Human-In-The-Loop.** A design principle: the system doesn't have to be either "fully
automatic" or "fully manual" — it can automate the easy, confident cases and route the
uncertain or high-stakes ones to a person. `backend/hitl/gate.py` implements this.

**The specific rule this project uses:** compute the *minimum* confidence across all findings
(not the average — one shaky finding shouldn't be hidden by three confident ones). If any
finding is CRITICAL severity, escalate to a human no matter how confident the model was — the
cost of missing something critical is judged too high to automate away. Otherwise, if overall
confidence clears a threshold (0.75 by default), post the review automatically; if not, route
to a pending human-approval queue.

**Why this matters as an interview point:** it's the direct, designed answer to "LLMs
hallucinate" — rather than pretending that doesn't happen, the system is architected around
the assumption that some fraction of findings will be wrong, and gates automation on
confidence and stakes rather than trusting every output blindly.

**Say this if asked:** "The gate uses the minimum confidence across findings, not the
average, so one uncertain finding can't be masked by more confident ones — and any CRITICAL
finding escalates to a human regardless of confidence, because that's a case where being
wrong is too costly to automate."

---

## 10. Talking to GitHub as a bot — GitHub Apps, JWT, installation tokens

**Why not just use a personal access token (PAT)?** A PAT acts as *you* — broad permissions
tied to your personal account, and it breaks if you ever leave. A **GitHub App** is a
separate identity altogether (this project's bot shows up as `aipr-review-agent-atulya[bot]`,
not as the human account), with permissions scoped to exactly the repos it's installed on.

**JWT (JSON Web Token):** a signed piece of text proving "I am who I say I am," verifiable
without the receiver needing to ask a third party — the signature is checked using
cryptographic math against a key only the real signer could have used. `backend/integrations/
github_client.py` signs a short-lived JWT (`_app_jwt`) using the GitHub App's private key
(`GITHUB_PRIVATE_KEY_PATH`) to prove "I am this App."

**Installation token:** the JWT itself isn't used for every API call — it's traded in for a
shorter-lived, narrower token scoped to one specific installation (one account's repos),
which is what actually authenticates each GitHub API request (fetching the diff, posting
the review).

**Say this if asked:** "The GitHub App signs a JWT with its private key to prove its identity,
then exchanges that for a scoped installation access token — that's what actually calls the
GitHub API to fetch diffs and post reviews, never the JWT directly."

---

## 11. Running AI locally for free — Ollama

**What it is:** software that runs LLMs directly on your own computer's hardware, instead of
calling a company's API over the internet. No API key, no per-call cost, no data leaving your
machine — the trade-off is you need a reasonably capable computer and the response is
generally slower and less capable than a large hosted model.

**Why this project uses it:** neither OpenAI nor Anthropic had billing configured on the
accounts available, and paying for API access wasn't an option. Ollama running `qwen2.5:14b-
instruct` (a 14-billion-parameter model — roughly, the number of internal tunable values;
bigger generally means more capable but slower) produced genuinely correct, well-reasoned
findings once given real retrieval and clear instructions — proof this doesn't strictly
require an expensive hosted model, though a hosted frontier model would likely be faster and
more consistent.

**Say this if asked:** "I ran this entirely on local inference via Ollama, with zero API
cost — I tried a 3B model first, which hallucinated more, and moved up to 14B, which
reliably produced correct, precisely-cited findings once the prompts explicitly instructed
it to cross-check retrieved context."

**Why local Ollama alone can't just be "deployed" as-is:** it needs the model's full weights
loaded into RAM (several GB for a 14B model), which free-tier cloud hosting doesn't provide.
The code also supports Groq (`backend/tools/llm_client.py`'s `GroqLLMClient`) — a free *hosted*
API that's a drop-in swap behind the same `LLMClient` interface, evaluated as the path to an
always-on deployment without paying for compute, though not put into live use.

---

## 12. Making the system fail safely — timeouts, retries, circuit breakers

Three separate, standard reliability patterns, each solving a different failure mode
(`backend/reliability/`):

- **Timeout:** put a maximum wait time on any call to something external. Without one, a
  single hung network call could stall the program forever.
- **Retry (with backoff):** if a call fails, try again a few times, waiting a bit longer each
  time, before giving up — handles brief, transient failures without giving up too early or
  hammering a struggling service too fast.
- **Circuit breaker:** if a dependency has failed repeatedly, stop even trying for a while and
  fail immediately instead — protects against piling up more slow failures on something that's
  clearly down, the same idea as an electrical circuit breaker tripping to stop a surge.

**Why this matters as a live example:** the demo feature deliberately built without these
(a raw `requests.post` with no timeout) actually crashed a real running job in testing — not
a hypothetical, an observed crash — which is exactly the failure these three patterns exist
to prevent.

**Say this if asked:** "Every outbound call — LLM, GitHub API, database — goes through a
timeout, then retry-with-backoff, then a circuit breaker if it keeps failing. I have a live
example where skipping this on purpose caused a real crash in testing."

---

## 13. Verifying the code actually works — pytest, mypy, Docker

**pytest:** the standard Python testing tool. A "test" is a small script that runs some code
and asserts the result is what's expected; `pytest` finds and runs all of them
(`backend/tests/`) and reports pass/fail. **pytest-asyncio** is an add-on needed because this
codebase's functions are `async` (see section 3) — regular pytest doesn't know how to run
those without it.

**mypy:** Python doesn't require you to declare variable types, but this codebase does anyway
(`x: int`, `def f(x: str) -> bool`) for clarity and to catch mistakes. `mypy` reads those
declared types *without running the code* and flags places where they don't add up — e.g.,
passing a string where an int was declared. This project's mypy run is clean across the whole
codebase.

**Docker / docker-compose:** Docker packages a piece of software (like Postgres or Redis)
along with everything it needs to run into a portable "container," so you can run it
identically on any machine without manually installing and configuring it. `docker-compose.yml`
describes "start these two containers, on these ports" as one command
(`docker compose up -d`) instead of manual setup steps.

**Say this if asked:** "20 tests pass, mypy is clean across the codebase, and infra
dependencies (Postgres, Redis) run in Docker so the whole thing is reproducible on any
machine with one command."

---

## Quick-reference glossary

| Term | One sentence |
|---|---|
| Webhook | A URL a service calls automatically when something happens, instead of you polling it. |
| HMAC | A signature proving a message came from someone who shares a secret with you. |
| Idempotent | Doing it twice has the same effect as doing it once. |
| Queue (Redis/ARQ) | A waiting list of jobs a separate worker process works through, decoupling "acknowledge fast" from "do slow work." |
| LangGraph | A library for running multiple AI steps as a graph, including ones that run in parallel. |
| LangSmith | A hosted dashboard that traces a LangGraph run's actual node execution, prompts, and responses. |
| System / user prompt | The AI's role+rules, vs. the actual thing it's asked to look at. |
| Hallucination | An LLM confidently generating a plausible-sounding but false claim. |
| RAG | Fetch relevant facts first, then hand them to the model alongside the question. |
| Embedding | Text turned into a list of numbers such that similar meaning → similar numbers. |
| Vector similarity search | Finding the stored embeddings closest in "meaning-space" to a new one. |
| pgvector | A Postgres extension adding native vector storage and similarity search. |
| Hypertable | A Postgres table that's internally time-partitioned for fast recent-data queries. |
| HITL | Automating the confident/easy cases, routing uncertain or high-stakes ones to a human. |
| GitHub App | A scoped bot identity for GitHub, distinct from any human's personal account. |
| JWT | A signed token proving identity, verifiable without asking a third party. |
| Timeout / retry / circuit breaker | Cap the wait / try again with backoff / stop trying fast on repeated failure. |
| mypy | Checks that a Python codebase's declared types are consistent, without running it. |
| Docker | Packages software with everything it needs so it runs identically anywhere. |

---

## How to actually prepare, not just read this once

1. Read this doc once straight through.
2. Open each file it references and find the one sentence in this doc that describes it —
   confirm you can point at the code and say what it does.
3. Re-read `docs/INTERVIEW_PREP.md` — it should now make sense on a second read in a way it
   didn't on the first.
4. Practice saying the "say this if asked" lines out loud, in your own words, not memorized
   verbatim — an interviewer can tell the difference.
