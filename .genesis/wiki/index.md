# Wiki Index — AIPRReviewAgent

The project knowledge base. Same schema as the agentic-swe-kit wiki: concept pages in `concepts/`,
each with frontmatter and ≥2 `[[wikilinks]]`. The L3 RESEARCH loop writes here; G0 reads here first.

> **Read this file before any milestone (G0 step 1).** Pick candidate pages by name-matching the
> milestone's nouns, then drill in. The wiki is what prevents rebuilding work that already exists.

## Entities (the things this system has)
<!-- - [[concepts/<Entity>]] — one-line summary -->

## Concepts (how it works)
<!-- - [[concepts/<Concept>]] — one-line summary -->

## Sources (research distilled by L3)
<!-- - [[concepts/<source-slug>]] — one-line summary | filed <date> -->

## Seeded from agentic-swe-kit
Relevant global concept pages for this project's phases (pointers only — read on demand):
- `llmops-ai-agents/concepts/Multi-Agent-Orchestration.md` — the four-specialist fan-out + aggregator (M1)
- `llmops-ai-agents/concepts/Orchestrator-Worker-Architecture.md` — LangGraph orchestrator design (M1)
- `llmops-ai-agents/concepts/Parallel-and-Fan-Out-Agents.md` — Send API parallel fan-out pattern (M1)
- `llmops-ai-agents/concepts/RAG-Architecture.md` — hybrid retrieval over code_chunks (M2)
- `llmops-ai-agents/concepts/Observability-and-Cost-Control.md` — events spine, BudgetGuard (M3)
- `llmops-ai-agents/concepts/Production-Hardening.md` — reliability layer (M4)
- `llmops-ai-agents/concepts/Evaluation-Frameworks.md` — golden dataset + LLM-as-judge (M4)
- `distributed-systems/concepts/Fault-Tolerance.md` — retries, circuit breakers, timeouts (M4)
- `distributed-systems/concepts/Security-in-Distributed-Systems.md` — webhook trust boundary (M1/M4)
- `security-engineering/concepts/Access-Control.md` — RBAC on API routes (M4)
