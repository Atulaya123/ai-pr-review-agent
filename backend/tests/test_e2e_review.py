from uuid import uuid4

import pytest

from backend.core.workflow_engine import get_workflow_engine
from backend.database.repository import get_review, save_review_result
from backend.models.enums import ReviewOutcome, Severity
from backend.models.review import ReviewRequest, ReviewResult
from backend.tests.fixtures.vulnerable_diff import CLEAN_DIFF_FILES, VULNERABLE_DIFF_FILES

INITIAL_STATE = {
    "retrieved_context": "",
    "security_findings": [],
    "quality_findings": [],
    "tests_findings": [],
    "docs_findings": [],
    "findings": [],
    "overall_confidence": 1.0,
    "outcome": ReviewOutcome.APPROVED,
}


async def test_pipeline_flags_sql_injection_and_blocks():
    """M1 demo: a fixture diff with a known SQL-injection pattern goes in, and
    the aggregator comes back with a CRITICAL finding + CRITICAL_BLOCK outcome
    — end to end through the real LangGraph fan-out, using the mock LLM client
    (no API keys needed)."""
    request = ReviewRequest(repo="acme/demo", pr_number=1, head_sha="abc123", files=VULNERABLE_DIFF_FILES)
    engine = get_workflow_engine()

    final_state = await engine.run(str(request.review_id), {"request": request, **INITIAL_STATE})

    assert len(final_state["findings"]) == 1
    finding = final_state["findings"][0]
    assert finding.severity == Severity.CRITICAL
    assert finding.category == "injection"
    assert final_state["outcome"] == ReviewOutcome.CRITICAL_BLOCK


async def test_pipeline_clean_diff_approves():
    request = ReviewRequest(repo="acme/demo", pr_number=2, head_sha="def456", files=CLEAN_DIFF_FILES)
    engine = get_workflow_engine()

    final_state = await engine.run(str(request.review_id), {"request": request, **INITIAL_STATE})

    assert final_state["findings"] == []
    assert final_state["outcome"] == ReviewOutcome.APPROVED


@pytest.mark.usefixtures("db_session")
async def test_review_result_persists_and_is_readable(db_session):
    result = ReviewResult(
        review_id=uuid4(),
        findings=[],
        overall_confidence=1.0,
        outcome=ReviewOutcome.APPROVED,
        posted=True,
    )
    await save_review_result(db_session, "acme/demo", 3, "ghi789", result)

    fetched = await get_review(db_session, result.review_id)
    assert fetched is not None
    assert fetched.repo == "acme/demo"
    assert fetched.outcome == "approved"
