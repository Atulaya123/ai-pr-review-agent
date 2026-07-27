from backend.hitl.gate import apply_hitl_gate
from backend.models.enums import AgentType, ReviewOutcome, Severity
from backend.models.findings import Finding


def _finding(severity: Severity, confidence: float) -> Finding:
    return Finding(
        agent_type=AgentType.SECURITY,
        severity=severity,
        category="test",
        summary="s",
        file_path="f.py",
        line_start=1,
        line_end=1,
        confidence=confidence,
        rationale="r",
    )


def test_no_findings_approves():
    confidence, outcome = apply_hitl_gate([])
    assert outcome == ReviewOutcome.APPROVED
    assert confidence == 1.0


def test_critical_finding_blocks_regardless_of_confidence():
    findings = [_finding(Severity.CRITICAL, confidence=0.99)]
    _, outcome = apply_hitl_gate(findings)
    assert outcome == ReviewOutcome.CRITICAL_BLOCK


def test_low_confidence_non_critical_escalates():
    findings = [_finding(Severity.LOW, confidence=0.3)]
    _, outcome = apply_hitl_gate(findings, confidence_threshold=0.75)
    assert outcome == ReviewOutcome.ESCALATED


def test_high_confidence_non_critical_requests_changes():
    findings = [_finding(Severity.MEDIUM, confidence=0.9)]
    _, outcome = apply_hitl_gate(findings, confidence_threshold=0.75)
    assert outcome == ReviewOutcome.REQUEST_CHANGES
