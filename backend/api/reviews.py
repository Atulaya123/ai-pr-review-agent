from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.repository import get_review
from backend.database.session import get_session

router = APIRouter(prefix="/api/reviews")


@router.get("/{review_id}")
async def read_review(review_id: UUID, session: AsyncSession = Depends(get_session)):
    record = await get_review(session, review_id)
    if record is None:
        raise HTTPException(status_code=404, detail="review not found")
    return {
        "id": str(record.id),
        "repo": record.repo,
        "pr_number": record.pr_number,
        "outcome": record.outcome,
        "overall_confidence": record.overall_confidence,
        "posted": record.posted,
        "findings": [
            {
                "agent_type": f.agent_type,
                "severity": f.severity,
                "category": f.category,
                "summary": f.summary,
                "file_path": f.file_path,
                "confidence": f.confidence,
            }
            for f in record.findings
        ],
    }
