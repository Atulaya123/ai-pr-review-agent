# PLAN — AIPRReviewAgent

The machine-parseable implementation plan. Mirrors the milestone table in `DONE.html` (DONE.html is the
human/visual view; this is the one loops read). Sliced so each milestone ships in one L1 BUILD pass.

> Slicing rule: a milestone must have (a) a single clear outcome, (b) an exact **demo command** that
> proves it, and (c) a freeze boundary of files it may touch. If you can't write the demo command,
> the milestone is too vague — split it.

---

## Brainstorm (G0.5 — fill before slicing milestones)

> Three fundamentally different approaches to the cognitive job. Pick one. Record the rationale.
> This is the cheapest design decision — you haven't written a line of code yet.

### Approach A — LangGraph in-process orchestrator (chosen)
StateGraph with Send-API fan-out to 4 specialist nodes, checkpointed to Redis. Runs inside the same Python process as the API/worker.
- Strengths: zero extra infra, first-class parallel fan-out, tight LLM tool-calling integration, fast local iteration.
- Weaknesses: newer/less proven at very high concurrent-workflow scale than a dedicated durable-execution engine.

### Approach B — Temporal durable workflow engine
A separate Temporal server + worker processes drive the same 4-agent fan-out.
- Strengths: battle-hardened at scale (Uber/Netflix), very strong durability guarantees, built-in retries.
- Weaknesses: meaningful ops overhead (separate server, separate deploy) before we know our actual workflow shapes; not LLM-specific.

### Approach C — Hand-rolled asyncio fan-out, no orchestration framework
`asyncio.gather` over the 4 specialist coroutines with manual state passing.
- Strengths: no new dependency at all.
- Weaknesses: no checkpointing (a crash mid-review loses all progress), no built-in retry/backoff semantics, reinvents what LangGraph already gives us for free.

### Chosen: Approach A (LangGraph) — matches ADR-001 in the architecture doc. Hidden behind `core/workflow_engine.py` (run/resume/get_state) so Temporal can be swapped in later at Phase 13+ scale without touching any other module — the cheap decision now, the expensive one deferred behind a seam.

---

## Milestones

### M1 — Core review loop, local & mocked
- **Outcome:** A fixture PR diff goes in, a structured review (4 specialist findings, aggregated, HITL-gated) comes out — entirely local, LLM calls mockable, no live GitHub/Tiger credentials required.
- **Phase (swe-master):** 3 Backend & API, 4 Workflow Orchestration, 8 Multi-Agent Systems
- **Files / freeze boundary:** `backend/**`
- **Demo command:** `docker compose up -d && pytest backend/tests/test_e2e_review.py -v`
- **Success criteria:** webhook HMAC verified + idempotent; job enqueued to Redis/ARQ; LangGraph fans out to security/quality/tests/docs nodes in parallel; aggregator dedups + computes confidence + applies HITL gate; result persisted to Postgres; test asserts a Finding list with the L2 contract shape comes back for a known-vulnerable fixture diff.
- **Loops:** L1, L4
- **Skills:** canon + tdd + data-systems-engineering
- **Token budget:** 50000

### M2 — Live integrations: GitHub App + Tiger Cloud + OpenAI
- **Outcome:** The same pipeline runs against a real GitHub PR and a real Tiger Cloud database, with RAG grounding over the actual repo instead of a mocked context.
- **Phase:** 6 Memory Architecture, 13 Infrastructure, 14 Data Engineering
- **Files:** `backend/memory/**`, `backend/integrations/**`, `scripts/migrations/**`
- **Demo command:** Open a real PR on the demo repo → a structured review comment appears on it within 60s; `psql "$TIGER_DATABASE_URL" -c "select count(*) from code_chunks"` returns > 0
- **Success criteria:** GitHub App receives and verifies a live webhook; hybrid DiskANN+FTS retrieval returns top-k real code chunks; specialists reason over diff+retrieved-context, not diff alone; review posts via the GitHub API.
- **Loops:** L1, L3 (research), L4
- **Skills:** canon + tdd + production-readiness, llmops-ai-agents
- **Token budget:** 50000

### M3 — Observability, HITL dashboard, economics
- **Outcome:** Every agent action is a row in `agent_events`; a dashboard shows the HITL approval queue and per-agent cost/latency.
- **Phase:** 10 Observability, 16 Economics, 19 Human-in-the-Loop
- **Files:** `backend/observability/**`, `backend/economics/**`, `backend/hitl/**`, `frontend/**`
- **Demo command:** `curl localhost:8000/api/economics/summary` returns real cost/latency data; dashboard at `localhost:3000/hitl` shows a pending review
- **Success criteria:** trace viewer reconstructs a review end-to-end from `agent_events`; `agent_health_1m` continuous aggregate populated; BudgetGuard blocks a call when the daily cap is exceeded.
- **Loops:** L1, L2, L4
- **Skills:** canon + tdd + production-readiness, llmops-ai-agents
- **Token budget:** 50000

### M4 — Reliability, security hardening, eval harness
- **Outcome:** The system survives injected faults (timeouts, dead agents, duplicate webhooks) and has a written threat model + regression eval gate.
- **Phase:** 9 Evaluation, 11 Security, 12 Reliability
- **Files:** `backend/reliability/**`, `backend/security/**`, `backend/evaluation/**`
- **Demo command:** `pytest backend/tests/test_fault_injection.py backend/tests/test_security.py -v`
- **Success criteria:** circuit breaker opens on repeated LLM failures; duplicate webhook delivery is deduped; prompt-injection fixture (malicious diff content) does not alter agent behavior outside its findings; golden-dataset eval score above threshold blocks regressions.
- **Loops:** L1, L2, L3, L4
- **Skills:** canon + tdd + security-engineering, distributed-systems
- **Token budget:** 50000

---

## Progress (loops append here on milestone completion — newest last)

- 2026-07-27 — M1 built: ingress/HMAC/idempotency, LangGraph 4-specialist fan-out (mock LLM),
  aggregator + HITL gate, ARQ worker, Postgres persistence, GitHub client (code complete,
  not live-tested — no App credentials yet). Gates computed: `mypy backend` clean,
  `pytest backend/tests` 14/14 green, plus a live docker-compose + uvicorn + arq + curl run
  confirmed the whole pipeline end-to-end. **Not yet marked DONE** — awaiting a separate-model
  L4 VERIFY pass per KICKOFF.md before the M1 status pill flips in DONE.html.
