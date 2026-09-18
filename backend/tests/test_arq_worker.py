from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from backend.database.repository import save_review_result
from backend.job_queue.arq_worker import _clamp_findings_to_diff, run_review_job
from backend.models.enums import AgentType, ReviewOutcome, Severity
from backend.models.findings import Finding
from backend.models.review import DiffFile, ReviewResult

PATCH = """diff --git a/demo/vulnerable_lookup.py b/demo/vulnerable_lookup.py
new file mode 100644
index 0000000..1111111
--- /dev/null
+++ b/demo/vulnerable_lookup.py
@@ -0,0 +1,5 @@
+def get_user(request):
+    user_id = request.args.get("id")
+    query = f"SELECT * FROM users WHERE id = {user_id}"
+    result = db.execute(query)
+    return result
"""


def _finding(line_start: int, line_end: int) -> Finding:
    return Finding(
        agent_type=AgentType.DOCS,
        severity=Severity.CRITICAL,
        category="test",
        summary="s",
        file_path="demo/vulnerable_lookup.py",
        line_start=line_start,
        line_end=line_end,
        confidence=0.9,
        rationale="r",
    )


def test_out_of_range_line_end_gets_clamped():
    findings = [_finding(2, 6)]  # file only has 5 lines
    clamped = _clamp_findings_to_diff(findings, [DiffFile(path="demo/vulnerable_lookup.py", patch=PATCH)])
    assert clamped[0].line_start == 2
    assert clamped[0].line_end == 5


def test_in_range_finding_is_unchanged():
    findings = [_finding(2, 4)]
    clamped = _clamp_findings_to_diff(findings, [DiffFile(path="demo/vulnerable_lookup.py", patch=PATCH)])
    assert clamped[0].line_start == 2
    assert clamped[0].line_end == 4


def test_unknown_file_path_passes_through_unchanged():
    findings = [_finding(2, 999)]
    clamped = _clamp_findings_to_diff(
        [Finding(**{**findings[0].model_dump(), "file_path": "other.py"})],
        [DiffFile(path="demo/vulnerable_lookup.py", patch=PATCH)],
    )
    assert clamped[0].line_end == 999


@pytest.mark.usefixtures("db_session")
async def test_run_review_job_skips_duplicate_post_on_already_posted_commit(db_session):
    """Guards the exact bug in docs/INTERVIEW_PREP.md's bug list (#6): ARQ
    retries the whole job on any exception, including one thrown after a
    successful GitHub post. Without this check, a retry re-runs the LLM
    pipeline and posts a second, duplicate review for a commit already
    handled."""
    existing = ReviewResult(
        review_id=uuid4(), findings=[], overall_confidence=1.0, outcome=ReviewOutcome.APPROVED, posted=True
    )
    await save_review_result(db_session, "acme/demo", 7, "sha-already-posted", existing)

    github = AsyncMock()
    engine = AsyncMock()

    review_id = await run_review_job(
        {"github_client": github, "workflow_engine": engine},
        repo="acme/demo",
        pr_number=7,
        installation_id=123,
        head_sha="sha-already-posted",
        files=[],
    )

    assert review_id == str(existing.review_id)
    github.post_review.assert_not_called()
    github.request_human_review.assert_not_called()
    engine.run.assert_not_called()
