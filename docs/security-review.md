# OWASP Top 10 for LLM Applications security review

## Follow-up: finding 1 only

After the original audit, the user authorized fixing only the fail-open generation
parser. Malformed JSON, missing/non-list envelopes and invalid finding items now raise
`ModelResponseParseError` at `model.parse`; invalid items are no longer silently dropped.
`ReviewFailure` is a distinct failed-attempt model, and pending/failed outcomes cannot
be constructed as completed `ReviewResult` objects. Initial workflow state is pending,
not approved. The worker records `review.failed` in `agent_events` and propagates the
typed error before posting or storing a successful review. The original numbered audit
below remains a historical snapshot of `b57854d`.
Failed attempts are recorded in the business ledger/logs, not as completed
`PRReviewRecord` rows; the completed-review API therefore has no result for that attempt.

The two acceptance tests run the real graph/worker with scripted provider responses:
malformed output produces failed-generation/review events, no GitHub call, and no
completed review record; valid `{"findings":[]}` still produces a clean approval,
record and post. Schema-edge tests also reject invalid envelopes/items and partial
success. No retry is needed to make the failure explicit. Live-provider behavior remains
unverified. **Finding 2 is still open and was not changed.**

## Known unmitigated risks at the original audit

- **Original finding 1, subsequently fixed as above:** malformed JSON became an empty findings list.
  If the other specialists also return no findings, the gate can approve the review.
  Invalid output is not distinguished from a valid clean review.
- **Outbound-content disclosure boundary:** diffs and retrieved context reach hosted
  generation/embedding providers without application-level secret redaction. Ordinary
  log redaction does not protect those requests or optional execution traces. Actual
  sensitive-data transmission, provider retention, and deployment consent are unverified.
- **Review lookup authorization:** the review API has no caller/tenant authorization.
  Someone who obtains a review UUID can retrieve its findings. UUID knowledge is a
  prerequisite; public UUID enumeration was not demonstrated.
- **Repository-scoped MCP authorization:** one bearer secret grants access to tools
  accepting caller-selected repositories; possession is not checked against repository
  permissions. Cross-tenant exposure depends on the deployed corpus/client boundaries.
- **Supply-chain reproducibility and consumption gaps:** production dependency ranges
  are not a reproducible integrity-checked lock. MCP retrieval embeddings do not use the
  generation cost ledger or an evident per-caller usage quota.

These are open risks, not claims that a compromise has occurred. This item changes
documentation only; none of the risks below was fixed.

## Scope and evidence

Source reviewed at commit `b57854d`, after Items 9-11. Framework:
[OWASP Top 10 for LLM Applications, 2025 edition](https://genai.owasp.org/resource/owasp-top-10-for-llm-applications-2025/),
with category IDs verified against the
[official 2.0 source](https://github.com/OWASP/www-project-top-10-for-large-language-model-applications/tree/main/2_0_vulns).
This is the 2025 list, not the differently numbered 2023-24 list.

The audit inspected source and existing tests, not excluded environment files or
production data. It performed no exploitation, live-provider calls, or production
permission checks. References below are repository-relative paths and line ranges at
the audited commit. "Addressed: partial" means specific controls exist, not that the
whole OWASP category is mitigated.

The local suite passed all 109 collected tests before this audit. That denominator
includes mocked, scripted-judge, replay, and local database/Redis tests; it does not
represent 109 production security trials.

## Source-backed findings

| # | Severity | File | Lines | Vulnerability | Confidence |
|---|----------|------|-------|---------------|------------|
| 1 | HIGH | `backend/agents/base_agent.py` | 220-230 | Invalid JSON becomes no findings, allowing a false clean review | 9/10 |
| 2 | MEDIUM | `backend/agents/base_agent.py`; `backend/tools/llm_client.py`; `backend/memory/embedder.py` | 62-78; 150-179; 17-29 | No application-level secret redaction before hosted generation/embedding requests | 8/10 |

Finding 1 follows through `backend/hitl/gate.py:6-11,28-39` and the enabled posting
path in `backend/job_queue/arq_worker.py:258-280`. A malformed response alone does not
erase another specialist's valid finding; the false approval requires the aggregate to
be empty. A posting kill switch prevents publication but does not repair the verdict.

Finding 2 is a source-confirmed missing data control, not proof of unauthorized provider
use or an actual credential leak. Hosted processing may be intentional and authorized;
the unresolved question is what data may leave, with whose consent, and under which
provider/tracing retention controls.

## LLM01: Prompt Injection

**Applies: yes. Addressed: partial. Unaddressed: yes.**

PR diffs, paths, commit messages and retrieved text cross an untrusted-content boundary.
`backend/security/injection_guard.py:8-46,84-109` detects selected instruction-shaped,
role-override, exfiltration, suppression and delimiter patterns. Prompt assembly fences
diffs, messages and retrieved context in `backend/agents/base_agent.py:62-78`.

These mechanisms do not establish that a live model ignores an attack. Pattern matching
can miss paraphrases and obfuscation; fencing retrieved text does not make its contents
trustworthy. `backend/observability/safety_metrics.py:35-36` explicitly limits the
measurement to sanitizer detection and prompt leakage, not model compliance. The
behavioral defense test uses an instruction-following mock. Fixed responses in
`backend/evaluation/replay.py:148-180,194-205` cannot establish live attack resistance.

## LLM02: Sensitive Information Disclosure

**Applies: yes. Addressed: partial. Unaddressed: yes.**

`backend/observability/logging.py:50-90` omits free-form messages, arguments, raw outputs
and unknown extras from structured logs and redacts selected secret patterns. This
reduces ordinary logging exposure; it is not a universal data-loss prevention policy.

Prompts contain diff/context (`backend/agents/base_agent.py:62-78`), Groq receives
messages (`backend/tools/llm_client.py:150-179`), and Gemini receives embedding text
(`backend/memory/embedder.py:17-29`). There is no corresponding outbound secret scrubber.
`render.yaml:18-27,45-50` configures hosted providers and tracing, but a manifest setting
does not prove active tracing or establish what the deployed SDK captured. Provider
retention, private-repository consent, trace filtering and deletion policies are
unverified. `backend/api/reviews.py:12-35` also returns stored findings without caller
authorization; access requires knowing a review UUID.

## LLM03: Supply Chain

**Applies: yes. Addressed: partial. Unaddressed: yes.**

The service trusts provider SDKs, graph/checkpoint libraries, embedding services and
evaluation packages. Some development dependencies are pinned, including RAGAS and
Black (`backend/requirements-dev.txt:1-6`). CI limits token permissions to contents read
(`.github/workflows/ci.yml:7-8`).

Production dependencies mostly use open-ended minimum versions
(`backend/requirements.txt:1-29`), installed without a lock or package hashes
(`.github/workflows/ci.yml:20-21`). Version pins on a few evaluation packages do not
establish integrity of the full dependency tree. Reproducible deployment resolution,
dependency provenance and deployed-vulnerability status were not verified; no compromised
dependency was identified by this audit.

## LLM04: Data and Model Poisoning

**Applies: yes for retrieval data; no training/fine-tuning pipeline was found.
Addressed: partial. Unaddressed: yes.**

Current ingestion uses a fixed first-party architecture chunk list rather than crawling
arbitrary PR contents (`scripts/ingest_docs.py:20-81`;
`airflow/dags/knowledge_base_refresh.py:49-51,104-121`). Parameterized storage writes
reduce SQL injection risk (`backend/memory/tiger_client.py:39-66`). Content hashes
provide refresh idempotency, and the existing regression gate checks retrieval behavior.

Neither idempotency nor a retrieval regression score establishes that source content is
benign. Corpus trust depends on source/deployment access; no separate corpus provenance,
approval or poisoning-defense mechanism was established. A trusted source change or
database writer can still insert misleading context. No poisoned production chunk was
observed, and the audit did not attempt corpus modification.

## LLM05: Improper Output Handling

**Applies: yes. Addressed: partial. Unaddressed: yes.**

The Groq call requests JSON (`backend/tools/llm_client.py:174-179`), and findings are
parsed into Pydantic models (`backend/agents/base_agent.py:232-261`). Citation checks
validate repository paths, line ranges and supplied quoted evidence
(`backend/verification/citations.py:17-64,88-94`). Findings are rendered into GitHub
Markdown rather than executed as shell commands
(`backend/integrations/github_client.py:136-156`).

Invalid JSON nevertheless returns `[]` (`backend/agents/base_agent.py:220-230`):
the fail-open clean-verdict path remains unmitigated. Schema/citation validity also
does not validate the meaning of a rationale or make model-authored Markdown trustworthy.
No GitHub Markdown exploit was demonstrated; GitHub's renderer is a separate boundary.

## LLM06: Excessive Agency

**Applies: yes. Addressed: partial. Unaddressed: yes.**

The exposed MCP toolset is read-only, and MCP is not mounted without its shared secret
(`backend/mcp_server/server.py:29-40,83-93`;
`backend/mcp_server/auth.py:16-36`). Review posting can be suppressed independently
of review execution (`backend/job_queue/arq_worker.py:258-280`).
Force-HITL overrides finding routing (`backend/orchestrator/nodes.py:191-194`) without
changing the calibrated threshold or minimum-confidence aggregation.

Normal operation still posts model-generated review content without universal human
approval. Importantly, the ordinary critical-finding outcome is `CRITICAL_BLOCK`, not
`ESCALATED` (`backend/hitl/gate.py:28-39`): it can be posted as a request-changes review,
not automatically placed in the human queue. Force-HITL changes that routing when enabled.
The GitHub App's actual permission scope was not verified. MCP's shared credential
does not enforce per-repository permissions, and Redis control-writer authorization
and auditability are deployment-dependent.

## LLM07: System Prompt Leakage

**Applies: yes as an extraction surface, with limited confidentiality impact.
Addressed: partial. Unaddressed: yes.**

Prompts are source-controlled templates (`backend/prompts/registry.py:14-24`), and
the security prompt explicitly labels diff/context as untrusted
(`backend/prompts/templates/security.txt:7-9`). Selected prompt-exfiltration wording
is detected (`backend/security/injection_guard.py:25-29`).

Public template text should be treated as non-secret; an extracted public prompt is
not itself a credential leak. No live-model extraction resistance has been measured.
If future deployment-specific instructions contain confidential policy, that
confidentiality remains unprotected by evidence. Prompt secrecy must not substitute
for authentication, repository authorization or the cost ledger.

## LLM08: Vector and Embedding Weaknesses

**Applies: yes. Addressed: partial. Unaddressed: yes.**

Parameterized retrieval queries filter by repository
(`backend/memory/tiger_client.py:111-131`). Current ingestion names one repository
(`scripts/ingest_docs.py:20`). Dense retrieval accepts the diff document; sparse
retrieval constructs a bounded OR query rather than AND-ing the whole document.

A repository filter is not caller authorization: MCP lets its authenticated caller
choose the repository (`backend/mcp_server/server.py:29-40`). Corpus/database access
and tenant boundaries need deployment verification. Embedding-service data exposure
is also covered under LLM02.

The supplied production measurement was **one pre-fix query**: vector lane 6 rows,
full-text lane 0 rows, overlap 0, fused output 5 rows. It does not establish an empty
index or all-query behavior. The local sparse fix added a candidate in **one of three
replay fixtures**, against **six checked-in chunks** with fixed vector ordering.
Post-fix production lane contributions and downstream live-model benefit are unverified.
The sparse scorer is PostgreSQL `ts_rank`, not BM25.

## LLM09: Misinformation

**Applies: yes. Addressed: partial. Unaddressed: yes.**

Citation failures lower finding confidence to zero before the existing gate
(`backend/orchestrator/nodes.py:170-187`);
`backend/verification/citations.py:17-64` checks grounding locations. The gate uses
the minimum finding confidence and the calibrated threshold
(`backend/hitl/gate.py:6-39`). Evaluation now reports retrieval metrics, the existing
judge rubrics and separate RAGAS scores rather than blending unlike measures.

A valid path/range is not proof that the reported defect exists. Confidence is
model-reported; fixed generation fixtures (`backend/evaluation/dataset.py:75-140`)
are not a representative sample of live provider behavior. Replays hold responses
fixed, so changed retrieval cannot improve their findings: they detect regression,
not live-model benefit. RAGAS judges also can be wrong; applicability states and judge
budgets do not make their conclusions ground truth. Live scores and current semantic
false-positive/false-negative rates remain unverified. The malformed-output clean
verdict under LLM05 is another concrete misinformation path.

## LLM10: Unbounded Consumption

**Applies: yes. Addressed: partial. Unaddressed: yes.**

Generation calls reserve against daily budgets before dispatch
(`backend/database/cost_ledger.py:77-163`), with separate agent/judge budgets and
unknown usage represented as unknown cost rather than zero. Output-token limits,
Groq pacing, job timeout and webhook body limits bound parts of the workload
(`backend/core/config.py:63-70`). Webhooks require configured HMAC verification
(`backend/webhook_receiver/validator.py:18-27`). Whole-file chunking fails explicitly
when a file cannot fit; hidden Groq SDK retries are disabled.

MCP retrieval calls the embedding path without an evident per-caller quota or
generation-ledger reservation (`backend/mcp_server/tools.py:18-28`). The evaluation
embedding adapter's judge reservations do not cover this ordinary retrieval path.
A daily generation budget is not a limit on queue growth, embedding requests, or
database/worker exhaustion. Local token estimates and retained unknown-cost
reservations are accounting controls, not a proven exact provider-invoice ceiling.
The chunking regression used synthetic responses for **eight calls: four specialists
with two chunks each**, not live Groq success. Production admission limits, provider
usage and Redis durability/authorization remain unverified.

## Deployment-dependent boundaries still to verify

`render.yaml:52-57` declares an all-IP Redis allowlist. That source setting alone
does not prove an unauthenticated public Redis: deployed authentication, TLS, network
reachability and ACLs were not inspected. Database isolation, GitHub App permissions,
MCP client/repository scope, runtime-control writers, and provider/LangSmith retention
likewise remain unverified. The ordinary JSON logger's reconstruction and redaction
tests prove behavior at that logger boundary, not sanitization of every external SDK.
