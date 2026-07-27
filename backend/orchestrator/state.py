from typing import TypedDict

from backend.models.enums import ReviewOutcome
from backend.models.findings import Finding
from backend.models.review import ReviewRequest


class ReviewState(TypedDict):
    request: ReviewRequest
    retrieved_context: str
    security_findings: list[Finding]
    quality_findings: list[Finding]
    tests_findings: list[Finding]
    docs_findings: list[Finding]
    findings: list[Finding]
    overall_confidence: float
    outcome: ReviewOutcome
