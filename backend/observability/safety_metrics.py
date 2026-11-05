from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.models import AgentEvent

SafetyApplicability = Literal["applicable", "not_applicable"]


@dataclass(frozen=True)
class SafetyRunAssessment:
    applicability: SafetyApplicability
    injection_attempt_count: int
    prompt_leak_count: int
    neutralized_count: int


@dataclass(frozen=True)
class SafetyMetrics:
    applicable_runs: int
    not_applicable_runs: int
    injection_attempt_runs: int
    prompt_leak_runs: int
    injection_attempts: int
    neutralized_attempts: int
    injection_attempt_rate: float | None
    prompt_leak_rate: float | None
    neutralization_rate: float | None


def calculate_safety_metrics(assessments: list[SafetyRunAssessment]) -> SafetyMetrics:
    """Measure detected injection and sanitizer leakage, not model compliance."""
    applicable = [item for item in assessments if item.applicability == "applicable"]
    not_applicable_runs = len(assessments) - len(applicable)
    injection_attempt_runs = sum(item.injection_attempt_count > 0 for item in applicable)
    prompt_leak_runs = sum(item.prompt_leak_count > 0 for item in applicable)
    injection_attempts = sum(item.injection_attempt_count for item in applicable)
    neutralized_attempts = sum(item.neutralized_count for item in applicable)

    return SafetyMetrics(
        applicable_runs=len(applicable),
        not_applicable_runs=not_applicable_runs,
        injection_attempt_runs=injection_attempt_runs,
        prompt_leak_runs=prompt_leak_runs,
        injection_attempts=injection_attempts,
        neutralized_attempts=neutralized_attempts,
        injection_attempt_rate=(injection_attempt_runs / len(applicable) if applicable else None),
        prompt_leak_rate=prompt_leak_runs / len(applicable) if applicable else None,
        neutralization_rate=(
            neutralized_attempts / injection_attempts if injection_attempts else None
        ),
    )


async def load_safety_metrics(
    session: AsyncSession, *, start: datetime, end: datetime
) -> SafetyMetrics:
    """Report injection-safety rates from the persisted business event ledger."""
    statement = select(AgentEvent).where(
        AgentEvent.event_type == "safety.assessment",
        AgentEvent.ts >= start,
        AgentEvent.ts < end,
    )
    events = (await session.execute(statement)).scalars().all()
    assessments_by_review: dict[UUID, SafetyRunAssessment] = {}
    for event in events:
        payload = event.payload or {}
        if event.outcome not in ("applicable", "not_applicable"):
            continue
        applicability: SafetyApplicability = (
            "applicable" if event.outcome == "applicable" else "not_applicable"
        )
        assessments_by_review[event.review_id] = SafetyRunAssessment(
            applicability=applicability,
            injection_attempt_count=_nonnegative_int(payload.get("injection_attempt_count")),
            prompt_leak_count=_nonnegative_int(payload.get("prompt_leak_count")),
            neutralized_count=_nonnegative_int(payload.get("neutralized_count")),
        )
    return calculate_safety_metrics(list(assessments_by_review.values()))


def _nonnegative_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
