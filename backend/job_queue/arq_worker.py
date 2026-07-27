import logging
from functools import lru_cache
from uuid import UUID

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from backend.core.config import get_settings
from backend.core.workflow_engine import get_workflow_engine
from backend.database.repository import save_review_result
from backend.database.session import get_sessionmaker
from backend.integrations.github_client import GitHubClient
from backend.models.enums import ReviewOutcome
from backend.models.review import DiffFile, ReviewRequest, ReviewResult

logger = logging.getLogger(__name__)


def _redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(get_settings().redis_url)


@lru_cache
def _redis_settings_cached() -> RedisSettings:
    return _redis_settings()


async def get_redis_pool() -> ArqRedis:
    return await create_pool(_redis_settings_cached())


async def run_review_job(
    ctx: dict,
    *,
    repo: str,
    pr_number: int,
    installation_id: int | None,
    head_sha: str,
    files: list[dict],
) -> str:
    """The ARQ task the worker executes for every enqueued review.

    ctx["github_client"] / ctx["workflow_engine"] are injected in on_startup so
    tests can substitute fakes without monkeypatching module globals.
    """
    github: GitHubClient = ctx.get("github_client") or GitHubClient()
    engine = ctx.get("workflow_engine") or get_workflow_engine()

    request = ReviewRequest(
        repo=repo,
        pr_number=pr_number,
        installation_id=installation_id,
        head_sha=head_sha,
        files=[DiffFile(**f) for f in files],
    )

    final_state = await engine.run(
        str(request.review_id),
        {
            "request": request,
            "retrieved_context": "",
            "security_findings": [],
            "quality_findings": [],
            "tests_findings": [],
            "docs_findings": [],
            "findings": [],
            "overall_confidence": 1.0,
            "outcome": ReviewOutcome.APPROVED,
        },
    )

    result = ReviewResult(
        review_id=request.review_id,
        findings=final_state["findings"],
        overall_confidence=final_state["overall_confidence"],
        outcome=final_state["outcome"],
    )

    if result.outcome != ReviewOutcome.ESCALATED and installation_id is not None:
        try:
            await github.post_review(repo, pr_number, installation_id, result.findings, result.outcome)
            result.posted = True
        except Exception:
            logger.exception("failed to post review for %s#%s", repo, pr_number)

    async with get_sessionmaker()() as session:
        await save_review_result(session, repo, pr_number, head_sha, result)

    return str(request.review_id)


async def _on_startup(ctx: dict) -> None:
    ctx["github_client"] = GitHubClient()
    ctx["workflow_engine"] = get_workflow_engine()


class WorkerSettings:
    functions = [run_review_job]
    on_startup = _on_startup
    redis_settings = _redis_settings()
