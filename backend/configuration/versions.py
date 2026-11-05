import hashlib
import json
import os
import subprocess
from functools import lru_cache
from inspect import signature
from pathlib import Path
from typing import Any

from backend.core.config import Settings, get_settings
from backend.prompts.registry import prompt_hash, prompt_library_hash

_REPO_ROOT = Path(__file__).resolve().parents[2]


@lru_cache
def _file_hash(relative_path: str) -> str:
    return hashlib.sha256((_REPO_ROOT / relative_path).read_bytes()).hexdigest()


@lru_cache
def _code_commit() -> str:
    configured_sha = os.environ.get("AIPR_CODE_COMMIT") or os.environ.get("GITHUB_SHA")
    if configured_sha:
        return configured_sha
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=_REPO_ROOT, text=True, timeout=1
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


@lru_cache
def _working_tree_state() -> str:
    try:
        changes = subprocess.check_output(
            ["git", "status", "--porcelain"],
            cwd=_REPO_ROOT,
            text=True,
            timeout=1,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return "dirty" if changes.strip() else "clean"


def build_behavior_manifest(
    *,
    agent: str | None = None,
    model: str | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Return a content-addressed snapshot of behavior-affecting configuration."""
    active_settings = settings or get_settings()
    if model is None:
        from backend.agents.base_agent import resolve_model_for_provider

        model = resolve_model_for_provider(active_settings.llm_provider)

    from backend.memory.tiger_client import RRF_K
    from backend.mcp_server.tools import get_findings_impl, retrieve_context_impl

    tool_schema = {
        function.__name__: str(signature(function))
        for function in (get_findings_impl, retrieve_context_impl)
    }
    tool_schema_hash = _digest(tool_schema)
    prompt_hashes = {
        prompt_name: prompt_hash(prompt_name)
        for prompt_name in ("security", "quality", "tests", "docs")
    }
    settings_snapshot = {
        "llm_provider": active_settings.llm_provider,
        "model_identifier": model,
        "max_output_tokens": active_settings.llm_max_output_tokens,
        "daily_budget_usd": active_settings.daily_budget_usd,
        "daily_judge_budget_usd": active_settings.daily_judge_budget_usd,
        "embedding_provider": active_settings.embedding_provider,
        "embedding_model": active_settings.embedding_model,
        "embedding_dims": active_settings.embedding_dims,
        "hitl_confidence_threshold": active_settings.hitl_confidence_threshold,
    }
    retrieval_configuration = {
        "strategy": "vector+full_text_rrf",
        "top_k": 5,
        "lane_k": 20,
        "rrf_k": RRF_K,
        "embedding_provider": active_settings.embedding_provider,
        "embedding_model": active_settings.embedding_model,
        "embedding_dims": active_settings.embedding_dims,
    }
    manifest: dict[str, Any] = {
        "code_commit": _code_commit(),
        "working_tree_state": _working_tree_state(),
        "model_settings": settings_snapshot,
        "prompt_hashes": prompt_hashes,
        "prompt_library_hash": prompt_library_hash(),
        "active_prompt_hash": prompt_hashes.get(agent) if agent else None,
        "tool_schema_hash": tool_schema_hash,
        "retrieval_configuration": retrieval_configuration,
        "guardrail_rules_hash": _digest(
            {
                path: _file_hash(path)
                for path in ("backend/security/injection_guard.py", "backend/hitl/gate.py")
            }
        ),
        "agent_graph_hash": _digest(
            {
                path: _file_hash(path)
                for path in ("backend/orchestrator/graph.py", "backend/orchestrator/nodes.py")
            }
        ),
    }
    manifest["configuration_hash"] = _digest(manifest)
    return manifest


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def prompt_hashes() -> dict[str, str]:
    """Expose prompt hashes for experiment manifests without returning prompt text."""
    return {name: prompt_hash(name) for name in ("security", "quality", "tests", "docs")}
