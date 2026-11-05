from uuid import UUID, uuid4
from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator

from backend.models.enums import ReviewOutcome
from backend.models.findings import Finding


class DiffFile(BaseModel):
    path: str
    patch: str  # unified diff hunk for this file


class ReviewRequest(BaseModel):
    """Input to the orchestrator — everything a specialist needs about the PR."""

    review_id: UUID = Field(default_factory=uuid4)
    attempt_id: UUID = Field(default_factory=uuid4)
    trace_id: UUID = Field(default_factory=uuid4)
    repo: str
    pr_number: int
    installation_id: int | None = None
    head_sha: str
    files: list[DiffFile]
    commit_messages: list[str] = Field(default_factory=list)


class ReviewResult(BaseModel):
    review_id: UUID
    findings: list[Finding]
    overall_confidence: float
    outcome: ReviewOutcome
    posted: bool = False

    @model_validator(mode="after")
    def require_completed_review(self) -> Self:
        if self.outcome in (ReviewOutcome.PENDING, ReviewOutcome.FAILED):
            raise ValueError("Incomplete or failed attempts are not completed review results")
        return self


class ReviewFailure(BaseModel):
    review_id: UUID
    attempt_id: UUID
    trace_id: UUID
    outcome: Literal[ReviewOutcome.FAILED] = ReviewOutcome.FAILED
    stage: Literal["model.parse"] = "model.parse"
    cause_type: str
