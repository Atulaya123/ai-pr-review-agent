import json
import logging
import re
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from collections.abc import Iterator
from typing import Any
from uuid import UUID

from arq.logs import default_log_config

from backend.configuration.versions import build_behavior_manifest
from backend.models.review import ReviewRequest

_context: ContextVar[dict[str, object]] = ContextVar("review_log_context", default={})
_FIELDS = {
    "review_id",
    "attempt_id",
    "trace_id",
    "agent",
    "finding_id",
    "finding_ids",
    "latency_ms",
    "tokens_in",
    "tokens_out",
    "cost_usd",
    "confidence",
    "vector_count",
    "fulltext_count",
    "overlap_count",
    "fused_count",
    "mode",
    "query_type",
    "outcome",
    "stage",
    "chunk_count",
    "file_count",
}
_logger = logging.getLogger("backend.review_trace")
_SECRET = re.compile(
    r"(?i)(?:bearer\s+\S+|gh[pousr]_[a-z0-9_]+|github_pat_[a-z0-9_]+|"
    r"gsk_[a-z0-9_]+|sk-[a-z0-9_-]+|AIza[a-z0-9_-]+|"
    r"[a-z]+://[^/\s]*:[^@\s]*@[^/\s]+)"
)


def _safe_value(value: object) -> object:
    if isinstance(value, dict):
        return {
            str(key): _safe_value(item)
            for key, item in value.items()
            if not re.search(
                r"(?i)(secret|password|api_key|authorization|private_key|diff|patch|raw_output)",
                str(key),
            )
        }
    if isinstance(value, (list, tuple)):
        return [_safe_value(item) for item in value]
    if isinstance(value, str):
        if "\n" in value or len(value) > 256 or "PRIVATE KEY" in value:
            return "[redacted]"
        return _SECRET.sub("[redacted]", value)
    return value


class ReviewJSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # Free-form messages, arguments, exception text and unknown extra fields
        # are deliberately omitted rather than relying on callers to redact.
        fields = dict(getattr(record, "review_context", _context.get()))
        supplied = getattr(record, "review_fields", {})
        if isinstance(supplied, dict):
            fields.update({key: value for key, value in supplied.items() if key in _FIELDS})
        output: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "event": getattr(record, "review_event", "application.log"),
            "attempt_id": fields.get("attempt_id"),
            "trace_id": fields.get("trace_id"),
            "behavior_versions": fields.pop("behavior_versions", {}),
        }
        output.update({key: value for key, value in fields.items() if key in _FIELDS})
        if record.exc_info and record.exc_info[0]:
            output["exception_type"] = record.exc_info[0].__name__
            exception = record.exc_info[1]
            output["exception_stage"] = getattr(exception, "stage", fields.get("stage"))
        return json.dumps(_safe_value(output), default=str)


@contextmanager
def review_log_context(request: ReviewRequest) -> Iterator[None]:
    token = _context.set(
        {
            "review_id": str(request.review_id),
            "attempt_id": str(request.attempt_id),
            "trace_id": str(request.trace_id),
            "behavior_versions": build_behavior_manifest(),
        }
    )
    try:
        yield
    finally:
        _context.reset(token)


def log_stage(event: str, *, request: ReviewRequest | None = None, **fields: object) -> None:
    if request is not None and not _context.get():
        with review_log_context(request):
            log_stage(event, request=None, **fields)
        return
    _logger.info(
        event,
        extra={
            "review_event": event,
            "review_fields": fields,
            "review_context": dict(_context.get()),
        },
    )


def _worker_log_config() -> dict[str, Any]:
    configuration = default_log_config(verbose=False)
    configuration["formatters"]["arq.standard"] = {
        "()": "backend.observability.logging.ReviewJSONFormatter"
    }
    configuration["loggers"]["backend"] = {
        "handlers": ["arq.standard"],
        "level": "INFO",
        "propagate": False,
    }
    configuration["loggers"]["backend.memory.tiger_client"] = {
        "level": "INFO",
        "propagate": True,
    }
    return configuration


WORKER_LOG_CONFIG = _worker_log_config()


def correlation_fields() -> dict[str, object]:
    return {
        key: value for key, value in _context.get().items() if key in ("attempt_id", "trace_id")
    }
