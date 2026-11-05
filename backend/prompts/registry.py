import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path
from collections.abc import Iterator, Mapping

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_PROMPT_NAMES = ("security", "quality", "tests", "docs")
_prompt_overrides: ContextVar[Mapping[str, str]] = ContextVar("prompt_overrides", default={})


def get_system_prompt(agent_type: str) -> str:
    override = _prompt_overrides.get().get(agent_type)
    if override is not None:
        return override
    return _load_system_prompt(agent_type)


@lru_cache
def _load_system_prompt(agent_type: str) -> str:
    path = _TEMPLATES_DIR / f"{agent_type}.txt"
    return path.read_text()


def prompt_hash(agent_type: str) -> str:
    return hashlib.sha256(get_system_prompt(agent_type).encode("utf-8")).hexdigest()


def prompt_library_hash() -> str:
    prompts = {name: prompt_hash(name) for name in _PROMPT_NAMES}
    encoded = json.dumps(prompts, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@contextmanager
def use_prompt_overrides(overrides: Mapping[str, str]) -> Iterator[None]:
    token = _prompt_overrides.set(dict(overrides))
    try:
        yield
    finally:
        _prompt_overrides.reset(token)
