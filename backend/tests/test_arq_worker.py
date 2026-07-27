from backend.job_queue.arq_worker import _clamp_findings_to_diff
from backend.models.enums import AgentType, Severity
from backend.models.findings import Finding
from backend.models.review import DiffFile

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
