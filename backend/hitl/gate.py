from backend.core.config import get_settings
from backend.models.enums import ReviewOutcome, Severity
from backend.models.findings import Finding


def compute_overall_confidence(findings: list[Finding]) -> float:
    """The aggregator's confidence is the weakest link, not the average — one
    low-confidence finding should not be masked by three high-confidence ones."""
    if not findings:
        return 1.0
    return min(f.confidence for f in findings)


def apply_hitl_gate(findings: list[Finding], *, confidence_threshold: float | None = None) -> tuple[float, ReviewOutcome]:
    """The L7 confidence-weighted HITL gate.

    - Any CRITICAL finding escalates regardless of confidence (consequence too high to automate).
    - Below-threshold confidence routes to the human approval queue.
    - Otherwise: auto-post (REQUEST_CHANGES if there's something to say, else APPROVED).
    """
    threshold = confidence_threshold if confidence_threshold is not None else get_settings().hitl_confidence_threshold
    overall_confidence = compute_overall_confidence(findings)

    if any(f.severity == Severity.CRITICAL for f in findings):
        return overall_confidence, ReviewOutcome.CRITICAL_BLOCK

    if overall_confidence < threshold:
        return overall_confidence, ReviewOutcome.ESCALATED

    if findings:
        return overall_confidence, ReviewOutcome.REQUEST_CHANGES

    return overall_confidence, ReviewOutcome.APPROVED
