from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from backend.models.enums import AgentType, Severity


class Finding(BaseModel):
    """The L2 contract: the unit that flows through the whole system."""

    id: UUID = Field(default_factory=uuid4)
    agent_type: AgentType
    severity: Severity
    category: str
    summary: str
    file_path: str
    line_start: int
    line_end: int
    suggestion: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str


class FindingList(BaseModel):
    agent_type: AgentType
    findings: list[Finding]
