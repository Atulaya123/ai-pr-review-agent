import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError, WatchError

from backend.core.config import get_settings
from backend.observability.logging import log_stage

CONTROL_KEY = "aipr:runtime_controls"


class RuntimeControls(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    posting_disabled: bool = False
    force_hitl: bool = False
    disabled_specialist: Literal["security", "quality", "tests", "docs"] | None = None

    @property
    def version(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


def _redis() -> Redis:
    settings = get_settings()
    return Redis.from_url(
        settings.redis_url,
        socket_timeout=settings.runtime_controls_timeout_s,
        socket_connect_timeout=settings.runtime_controls_timeout_s,
    )


async def get_runtime_controls() -> RuntimeControls:
    redis = _redis()
    try:
        raw = await redis.get(CONTROL_KEY)
        controls = (
            RuntimeControls.model_validate_json(raw) if raw is not None else RuntimeControls()
        )
        source = "redis" if raw is not None else "default"
    except (RedisError, OSError, ValidationError):
        controls = RuntimeControls(posting_disabled=True, force_hitl=True)
        source = "unavailable"
    finally:
        await redis.aclose()
    log_stage(
        "runtime.controls.loaded",
        stage="controls",
        outcome=source,
        runtime_controls=controls.model_dump(),
        runtime_controls_hash=controls.version,
    )
    return controls


async def update_runtime_controls(changes: dict[str, object]) -> RuntimeControls:
    """Apply a validated partial update without overwriting concurrent edits."""
    redis = _redis()
    try:
        async with redis.pipeline(transaction=True) as pipe:
            while True:
                try:
                    await pipe.watch(CONTROL_KEY)
                    raw = await pipe.get(CONTROL_KEY)
                    current = (
                        RuntimeControls.model_validate_json(raw)
                        if raw is not None
                        else RuntimeControls()
                    )
                    controls = RuntimeControls.model_validate({**current.model_dump(), **changes})
                    pipe.multi()
                    pipe.set(CONTROL_KEY, controls.model_dump_json())
                    await pipe.execute()
                    return controls
                except WatchError:
                    continue
    finally:
        await redis.aclose()
