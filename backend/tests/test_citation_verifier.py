import pytest

from backend.hitl.gate import apply_hitl_gate
from backend.models.enums import AgentType, ReviewOutcome, Severity
from backend.models.findings import Finding
from backend.models.review import DiffFile, ReviewRequest
from backend.orchestrator import nodes
from backend.orchestrator.state import ReviewState
from backend.verification.citations import verify_finding_citation

PATCH = """diff --git a/app.py b/app.py
@@ -8,2 +8,3 @@
 existing = True
-old_call()
+db.execute(query)
"""


def _finding(
    *,
    file_path: str = "app.py",
    line_start: int = 9,
    line_end: int = 9,
    evidence: str | None = None,
) -> Finding:
    return Finding(
        agent_type=AgentType.SECURITY,
        severity=Severity.HIGH,
        category="injection",
        summary="Unsafe query",
        file_path=file_path,
        line_start=line_start,
        line_end=line_end,
        evidence=evidence,
        confidence=0.99,
        rationale="The query is unsafe.",
    )


def test_verifies_path_range_and_exact_evidence_in_diff():
    result = verify_finding_citation(
        _finding(evidence="db.execute(query)"), [DiffFile(path="app.py", patch=PATCH)]
    )

    assert result.verified is True
    assert result.failures == ()


@pytest.mark.parametrize(
    ("finding", "repository_files", "expected_failure"),
    [
        (_finding(file_path="missing.py"), None, "could not be verified"),
        (_finding(line_start=20, line_end=20), None, "outside"),
        (_finding(evidence="db.execute(secret)"), None, "verbatim"),
        (
            _finding(file_path="src/helper.py", line_start=2, line_end=2, evidence="return None"),
            {"src/helper.py": "def helper():\n    return None"},
            None,
        ),
    ],
)
def test_verifier_handles_invalid_and_repository_citations(
    finding: Finding,
    repository_files: dict[str, str] | None,
    expected_failure: str | None,
):
    result = verify_finding_citation(
        finding, [DiffFile(path="app.py", patch=PATCH)], repository_files
    )

    if expected_failure is None:
        assert result.verified is True
    else:
        assert result.verified is False
        assert any(expected_failure in failure for failure in result.failures)


def test_rejects_non_repository_relative_citation_path():
    result = verify_finding_citation(
        _finding(file_path="../secret.py"), [DiffFile(path="app.py", patch=PATCH)]
    )

    assert result.verified is False
    assert "repository-relative" in result.failures[0]


def test_pr15_mcp_init_citation_is_rejected_independent_of_confidence() -> None:
    repository_files = {
        "backend/mcp_server/__init__.py": "",
        "backend/mcp_server/server.py": (
            "async def retrieve_context():\n"
            "    return None\n"
            "\n"
            "async def get_findings():\n"
            "    return None\n"
        ),
    }
    finding = _finding(
        file_path="backend/mcp_server/__init__.py",
        line_start=1,
        line_end=1,
    ).model_copy(update={"confidence": 0.78})
    result = verify_finding_citation(
        finding,
        [DiffFile(path="backend/mcp_server/server.py", patch=PATCH)],
        repository_files,
    )

    assert finding.confidence == 0.78
    assert "async def retrieve_context" in repository_files["backend/mcp_server/server.py"]
    assert "async def get_findings" in repository_files["backend/mcp_server/server.py"]
    assert result.verified is False
    assert any("outside" in failure for failure in result.failures)


async def test_verification_failure_lowers_confidence_before_existing_hitl_gate(
    monkeypatch: pytest.MonkeyPatch,
):
    request = ReviewRequest(
        repo="acme/demo",
        pr_number=1,
        head_sha="abc123",
        files=[DiffFile(path="app.py", patch=PATCH)],
    )
    finding = _finding(file_path="missing.py")
    emitted_events: list[str] = []

    async def record_event(*args, **kwargs) -> None:
        emitted_events.append(args[2])

    monkeypatch.setattr(nodes, "emit_agent_event", record_event)
    state: ReviewState = {
        "request": request,
        "retrieved_context": "",
        "security_findings": [finding],
        "quality_findings": [],
        "tests_findings": [],
        "docs_findings": [],
        "findings": [],
        "overall_confidence": 1.0,
        "outcome": ReviewOutcome.APPROVED,
    }

    result = await nodes.aggregate_node(state)

    assert result["findings"][0].confidence == 0.0
    assert result["overall_confidence"] == 0.0
    assert result["outcome"] == ReviewOutcome.ESCALATED
    assert result["outcome"] == apply_hitl_gate([finding.model_copy(update={"confidence": 0.0})])[1]
    assert emitted_events[0] == "finding.verification_failed"
