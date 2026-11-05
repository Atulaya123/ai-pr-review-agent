import logging
from datetime import datetime, timezone
from functools import lru_cache
from uuid import UUID, uuid4

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from backend.core.config import get_settings
from backend.configuration.runtime_controls import get_runtime_controls
from backend.core.workflow_engine import WorkflowEngine, get_workflow_engine
from backend.database.repository import enqueue_hitl_review, get_posted_review, save_review_result
from backend.database.session import get_sessionmaker
from backend.integrations.diff_parser import get_valid_line_range
from backend.integrations.github_client import GitHubClient
from backend.models.enums import ReviewOutcome
from backend.models.findings import Finding
from backend.models.review import DiffFile, ReviewFailure, ReviewRequest, ReviewResult
from backend.observability.events import emit_agent_event
from backend.observability.logging import log_stage, review_log_context
from backend.core.exceptions import ModelResponseParseError, StageExecutionError
from backend.observability.timing import (
    clock_domain_id,
    elapsed_ms,
    monotonic_now_ns,
    queue_elapsed_ms,
)

logger = logging.getLogger(__name__)


def _clamp_findings_to_diff(findings: list[Finding], files: list[DiffFile]) -> list[Finding]:
    """Clamp each finding's line range into what the diff actually contains.

    LLMs — especially small local ones — occasionally return a line just past
    the end of a short file. GitHub's review API 422s on an out-of-diff line,
    which would otherwise sink the whole review over one slightly-off finding.
    """
    patch_by_path = {f.path: f.patch for f in files}
    clamped = []
    for finding in findings:
        patch = patch_by_path.get(finding.file_path)
        valid_range = get_valid_line_range(patch) if patch else None
        if valid_range is None:
            clamped.append(finding)
            continue
        lo, hi = valid_range
        new_start = min(max(finding.line_start, lo), hi)
        new_end = min(max(finding.line_end, lo), hi)
        if new_end < new_start:
            new_start = new_end
        if (new_start, new_end) != (finding.line_start, finding.line_end):
            logger.warning(
                "clamped finding line range for %s from (%s,%s) to (%s,%s) — outside diff bounds (%s,%s)",
                finding.file_path,
                finding.line_start,
                finding.line_end,
                new_start,
                new_end,
                lo,
                hi,
            )
        clamped.append(finding.model_copy(update={"line_start": new_start, "line_end": new_end}))
    return clamped


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
    review_id: str | None = None,
    attempt_id: str | None = None,
    trace_id: str | None = None,
    enqueued_at_utc: str | None = None,
    enqueued_monotonic_ns: int | None = None,
    enqueue_clock_domain: str | None = None,
) -> str:
    """The ARQ task the worker executes for every enqueued review.

    ctx["github_client"] / ctx["workflow_engine"] are injected in on_startup so
    tests can substitute fakes without monkeypatching module globals.
    """
    started_ns = monotonic_now_ns()
    started_at = datetime.now(timezone.utc)
    request = ReviewRequest(
        review_id=UUID(review_id) if review_id is not None else uuid4(),
        attempt_id=(
            UUID(attempt_id) if attempt_id is not None and ctx.get("job_try", 1) == 1 else uuid4()
        ),
        trace_id=UUID(trace_id) if trace_id is not None else uuid4(),
        repo=repo,
        pr_number=pr_number,
        installation_id=installation_id,
        head_sha=head_sha,
        files=[DiffFile(**f) for f in files],
    )
    if enqueued_monotonic_ns is not None and enqueue_clock_domain is not None:
        queue_ms = queue_elapsed_ms(enqueued_monotonic_ns, enqueue_clock_domain, started_ns)
    else:
        queue_ms = None
    picked_up_at = datetime.now(timezone.utc)
    log_stage(
        "queue.completed",
        request=request,
        stage="queue",
        latency_ms=queue_ms,
        outcome="measured" if queue_ms is not None else "unavailable",
    )

    outcome = "success"
    try:
        await emit_agent_event(
            request.review_id,
            "worker",
            "queue.complete",
            occurred_at=picked_up_at,
            latency_ms=queue_ms,
            outcome="measured" if queue_ms is not None else "unavailable",
            payload={
                "enqueued_at_utc": enqueued_at_utc,
                "picked_up_at_utc": picked_up_at.isoformat(),
                "enqueue_clock_domain": enqueue_clock_domain,
                "worker_clock_domain": clock_domain_id(),
                "duration_clock": "monotonic",
                "attempt_id": str(request.attempt_id),
                "trace_id": str(request.trace_id),
            },
        )
        async with get_sessionmaker()() as session:
            already_posted = await get_posted_review(session, repo, pr_number, head_sha)
        if already_posted is not None:
            logger.info(
                "review already posted for %s#%s @ %s (review_id=%s) — skipping duplicate run",
                repo,
                pr_number,
                head_sha,
                already_posted.id,
            )
            outcome = "duplicate"
            return str(already_posted.id)

        github: GitHubClient = ctx.get("github_client") or GitHubClient()
        engine = ctx.get("workflow_engine") or get_workflow_engine()
        return await _execute_review_job(
            request=request,
            engine=engine,
            github=github,
            repo=repo,
            pr_number=pr_number,
            installation_id=installation_id,
            head_sha=head_sha,
        )
    except Exception:
        outcome = "error"
        raise
    finally:
        log_stage(
            "worker.completed",
            request=request,
            stage="execution",
            latency_ms=elapsed_ms(started_ns),
            outcome=outcome,
        )
        await emit_agent_event(
            request.review_id,
            "worker",
            "worker.active",
            latency_ms=elapsed_ms(started_ns),
            outcome=outcome,
            payload={
                "started_at_utc": started_at.isoformat(),
                "duration_clock": "monotonic",
                "attempt_id": str(request.attempt_id),
                "trace_id": str(request.trace_id),
            },
        )


async def _execute_review_job(
    *,
    request: ReviewRequest,
    engine: WorkflowEngine,
    github: GitHubClient,
    repo: str,
    pr_number: int,
    installation_id: int | None,
    head_sha: str,
) -> str:
    with review_log_context(request):
        log_stage("worker.started", stage="pickup")
        try:
            return await _execute_review_job_inner(
                request=request,
                engine=engine,
                github=github,
                repo=repo,
                pr_number=pr_number,
                installation_id=installation_id,
                head_sha=head_sha,
            )
        except ModelResponseParseError as exc:
            failure = ReviewFailure(
                review_id=request.review_id,
                attempt_id=request.attempt_id,
                trace_id=request.trace_id,
                cause_type=exc.cause_type,
            )
            await emit_agent_event(
                request.review_id,
                "worker",
                "review.failed",
                outcome=failure.outcome.value,
                payload=failure.model_dump(mode="json"),
            )
            log_stage("review.failed", stage=exc.stage, outcome=failure.outcome.value)
            raise
        except StageExecutionError as exc:
            log_stage("worker.failed", stage=exc.stage, outcome=exc.cause_type)
            raise
        except Exception as exc:
            log_stage("worker.failed", stage="workflow", outcome=type(exc).__name__)
            raise StageExecutionError("workflow", exc) from exc


async def _execute_review_job_inner(
    *,
    request: ReviewRequest,
    engine: WorkflowEngine,
    github: GitHubClient,
    repo: str,
    pr_number: int,
    installation_id: int | None,
    head_sha: str,
) -> str:
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
            "outcome": ReviewOutcome.PENDING,
        },
    )

    result = ReviewResult(
        review_id=request.review_id,
        findings=_clamp_findings_to_diff(final_state["findings"], request.files),
        overall_confidence=final_state["overall_confidence"],
        outcome=final_state["outcome"],
    )

    controls = await get_runtime_controls()
    if controls.posting_disabled:
        log_stage(
            "github.suppressed",
            stage="posting",
            outcome="kill_switch",
            runtime_controls=controls.model_dump(),
            runtime_controls_hash=controls.version,
        )
    if installation_id is not None and not controls.posting_disabled:
        try:
            if result.outcome == ReviewOutcome.ESCALATED:
                await github.request_human_review(
                    repo,
                    pr_number,
                    installation_id,
                    overall_confidence=result.overall_confidence,
                    findings=result.findings,
                )
            else:
                await github.post_review(
                    repo, pr_number, installation_id, result.findings, result.outcome
                )
            result.posted = True
            log_stage("github.posted", stage="posting", outcome=result.outcome.value)
        except Exception:
            logger.exception("failed to post review for %s#%s", repo, pr_number)
            log_stage("github.failed", stage="posting", outcome="failed")

    try:
        async with get_sessionmaker()() as session:
            await save_review_result(session, repo, pr_number, head_sha, result)
    except Exception as exc:
        raise StageExecutionError("persistence", exc) from exc
    log_stage(
        "review.recorded", stage="persistence", finding_ids=[str(f.id) for f in result.findings]
    )

    if result.outcome == ReviewOutcome.ESCALATED:
        reason = (
            "runtime force-HITL enabled"
            if final_state.get("gate_reason") == "force_hitl"
            else f"overall_confidence {result.overall_confidence:.2f} below threshold or verification failed"
        )
        async with get_sessionmaker()() as session:
            await enqueue_hitl_review(
                session,
                request.review_id,
                reason=reason,
            )
        await emit_agent_event(
            request.review_id,
            "hitl",
            "hitl.wait.started",
            outcome="pending",
            payload={"reason": "finding escalated; awaiting external human review"},
        )
        log_stage(
            "hitl.enqueued", stage="human_queue", finding_ids=[str(f.id) for f in result.findings]
        )

    await emit_agent_event(
        request.review_id,
        "worker",
        "review.complete",
        outcome=result.outcome.value,
        payload={"finding_count": len(result.findings)},
    )
    return str(request.review_id)


async def _on_startup(ctx: dict) -> None:
    ctx["github_client"] = GitHubClient()
    ctx["workflow_engine"] = get_workflow_engine()


class WorkerSettings:
    functions = [run_review_job]
    on_startup = _on_startup
    redis_settings = _redis_settings()
    job_timeout = get_settings().review_job_timeout_s
