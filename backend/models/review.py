from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from backend.models.enums import ReviewOutcome
from backend.models.findings import Finding


class DiffFile(BaseModel):
    path: str
    patch: str  # unified diff hunk for this file


class ReviewRequest(BaseModel):
    """Input to the orchestrator — everything a specialist needs about the PR."""

    review_id: UUID = Field(default_factory=uuid4)
    repo: str
    pr_number: int
    installation_id: int | None = None
    head_sha: str
    files: list[DiffFile]


class ReviewResult(BaseModel):
    review_id: UUID
    findings: list[Finding]
    overall_confidence: float
    outcome: ReviewOutcome
    posted: bool = False
