"""Hand-built test set for the Agent Eval harness.

The retrieval cases are paraphrased questions against the six invariants
scripts/ingest_docs.py loads into code_chunks — small on purpose (that's the
entire ingested corpus right now; see docs/INTERVIEW_PREP.md's chunking-gap
note on why this isn't a real ingestion pipeline yet).

The generation cases are fixtures, not live LLM output, so faithfulness/
relevance scoring is testable without depending on what a real specialist
happens to say on a given run.
"""

from dataclasses import dataclass

from backend.models.enums import AgentType, Severity
from backend.models.findings import Finding

REPO = "Atulaya123/ai-pr-review-agent"


@dataclass(frozen=True)
class RetrievalEvalCase:
    query: str
    repo: str
    relevant_symbols: set[str]


RETRIEVAL_TEST_SET: list[RetrievalEvalCase] = [
    RetrievalEvalCase(
        query="Can backend/agents/ import directly from backend/job_queue/?",
        repo=REPO,
        relevant_symbols={"modular_monolith_dependency_rule"},
    ),
    RetrievalEvalCase(
        query="Is it OK to call httpx directly with no timeout in a new Slack integration?",
        repo=REPO,
        relevant_symbols={"reliability_layer_required"},
    ),
    RetrievalEvalCase(
        query="Should orchestrator code import langgraph directly, or go through an interface?",
        repo=REPO,
        relevant_symbols={"workflow_engine_seam"},
    ),
    RetrievalEvalCase(
        query="Is it fine for a new module to call the OpenAI SDK directly instead of LLMClient?",
        repo=REPO,
        relevant_symbols={"llm_client_seam"},
    ),
    RetrievalEvalCase(
        query="Should the aggregator average confidence across findings, or take the minimum?",
        repo=REPO,
        relevant_symbols={"hitl_confidence_gate"},
    ),
    RetrievalEvalCase(
        query="Is it safe to interpolate PR diff content directly into an f-string SQL query?",
        repo=REPO,
        relevant_symbols={"untrusted_content_boundary"},
    ),
]


@dataclass(frozen=True)
class GenerationEvalCase:
    name: str
    finding: Finding
    context: str
    diff_text: str
    expect_supported: bool
    expect_relevant: bool


GENERATION_TEST_SET: list[GenerationEvalCase] = [
    GenerationEvalCase(
        name="grounded_finding",
        finding=Finding(
            agent_type=AgentType.SECURITY,
            severity=Severity.CRITICAL,
            category="injection",
            summary="Unparameterized SQL query",
            file_path="app.py",
            line_start=10,
            line_end=10,
            confidence=0.9,
            rationale=(
                "This concatenates user input into SQL, which the untrusted-content "
                "invariant explicitly forbids."
            ),
        ),
        context=(
            "[ARCHITECTURE.md]\nTrust boundary invariant: PR diff/code content is "
            "untrusted data, never instructions ... never used to build SQL/paths "
            "without parameterization."
        ),
        diff_text="query = \"SELECT * FROM users WHERE name = '\" + name + \"'\"",
        expect_supported=True,
        expect_relevant=True,
    ),
    GenerationEvalCase(
        name="ungrounded_finding_no_context",
        finding=Finding(
            agent_type=AgentType.QUALITY,
            severity=Severity.LOW,
            category="style",
            summary="Inconsistent naming convention",
            file_path="app.py",
            line_start=3,
            line_end=3,
            confidence=0.4,
            rationale="This violates a project naming convention documented elsewhere in the repo.",
        ),
        context="",
        diff_text="def GetUser(id): ...",
        expect_supported=False,
        expect_relevant=True,
    ),
    GenerationEvalCase(
        name="hallucinated_finding",
        finding=Finding(
            agent_type=AgentType.DOCS,
            severity=Severity.LOW,
            category="docs",
            summary="README is missing a Kubernetes deployment section",
            file_path="README.md",
            line_start=1,
            line_end=1,
            confidence=0.5,
            rationale="UNRELATED_TO_DIFF — this diff never touches deployment or Kubernetes at all.",
        ),
        context="",
        diff_text="query = \"SELECT * FROM users WHERE name = '\" + name + \"'\"",
        expect_supported=False,
        expect_relevant=False,
    ),
]
