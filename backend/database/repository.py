from uuid import UUID

from sqlalchemy import CursorResult, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.exceptions import DuplicateDeliveryError
from backend.database.models import FindingRecord, HITLReview, PRReviewRecord, WebhookDelivery
from backend.models.review import ReviewResult


async def claim_delivery(session: AsyncSession, delivery_id: str) -> None:
    """Insert-or-raise: the idempotency check from the ingress invariant.

    A unique-constraint violation means this delivery was already handled.
    """
    stmt = pg_insert(WebhookDelivery).values(delivery_id=delivery_id).on_conflict_do_nothing()
    result: CursorResult = await session.execute(stmt)  # type: ignore[assignment]
    await session.commit()
    if result.rowcount == 0:
        raise DuplicateDeliveryError(f"delivery {delivery_id} already processed")


async def save_review_result(session: AsyncSession, repo: str, pr_number: int, head_sha: str, result: ReviewResult) -> PRReviewRecord:
    record = PRReviewRecord(
        id=result.review_id,
        repo=repo,
        pr_number=pr_number,
        head_sha=head_sha,
        overall_confidence=result.overall_confidence,
        outcome=result.outcome.value,
        posted=result.posted,
    )
    for f in result.findings:
        record.findings.append(
            FindingRecord(
                id=f.id,
                agent_type=f.agent_type.value,
                severity=f.severity.value,
                category=f.category,
                summary=f.summary,
                file_path=f.file_path,
                line_start=f.line_start,
                line_end=f.line_end,
                suggestion=f.suggestion,
                confidence=f.confidence,
                rationale=f.rationale,
            )
        )
    session.add(record)
    await session.commit()
    await session.refresh(record)
    return record


async def enqueue_hitl_review(session: AsyncSession, review_id: UUID, reason: str) -> HITLReview:
    hitl = HITLReview(review_id=review_id, reason=reason)
    session.add(hitl)
    await session.commit()
    await session.refresh(hitl)
    return hitl


async def get_review(session: AsyncSession, review_id: UUID) -> PRReviewRecord | None:
    stmt = select(PRReviewRecord).where(PRReviewRecord.id == review_id)
    return (await session.execute(stmt)).scalar_one_or_none()
