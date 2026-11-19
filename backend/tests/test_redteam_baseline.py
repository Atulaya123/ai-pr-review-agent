import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
import requests

from backend.core.exceptions import BudgetExceededError
from backend.redteam.baseline import (
    BaselineRunAborted,
    JsonRecord,
    assert_comparable_baselines,
    merge_baseline_records,
    run_complete_baseline,
)
from backend.redteam.corpus import evaluate_persistent_influence
from backend.tools.llm_client import LLMClient, LLMResponse
from scripts import run_redteam_baseline as runner


def test_harness_disables_tracing_and_makes_no_outbound_trace_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeClient(LLMClient):
        async def complete_json(
            self,
            *,
            system: str,
            user: str,
            model: str,
            max_output_tokens: int = 2048,
            purpose: str = "agent",
            review_id=None,
            agent=None,
        ) -> LLMResponse:
            return LLMResponse(
                text='{"findings":[]}',
                tokens_in=1,
                tokens_out=1,
                cost_usd=0.0,
                model=model,
            )

    class FakeEngine:
        async def dispose(self) -> None:
            return None

    async def no_database() -> None:
        return None

    async def no_event(*_args, **_kwargs) -> None:
        return None

    attacks: list[JsonRecord] = [
        {
            "id": "RT-TEST",
            "family": "DIRECT_OVERRIDE",
            "success_condition": {
                "type": "output_contains",
                "target": "finding_text",
                "substring": "canary",
            },
            "artefact": {"files": [{"path": "src/a.py", "patch": "@@ -1 +1 @@\n+x"}]},
        }
    ]
    benign: list[JsonRecord] = [
        {
            "id": "BN-TEST",
            "hard_negative": True,
            "artefact": {"files": [{"path": "src/b.py", "patch": "@@ -1 +1 @@\n+y"}]},
        }
    ]
    monkeypatch.setattr(
        runner,
        "get_settings",
        lambda: SimpleNamespace(
            llm_provider="ollama",
            llm_max_output_tokens=2048,
            tiger_database_url="postgresql+asyncpg://postgres:postgres@127.0.0.1:5544/aipr_review_redteam",
        ),
    )
    monkeypatch.setattr(runner, "_initialize_local_database", no_database)
    monkeypatch.setattr(
        runner, "_read_jsonl", lambda _path: attacks if "attacks" in str(_path) else benign
    )
    monkeypatch.setattr(runner, "get_llm_client", lambda _settings: FakeClient())
    monkeypatch.setattr(runner, "get_engine", lambda: FakeEngine())
    monkeypatch.setattr(runner.nodes, "emit_agent_event", no_event)
    monkeypatch.setattr(runner.base_agent, "emit_agent_event", no_event)
    for name in runner._TRACING_ENV_VARS:
        monkeypatch.setenv(name, "true")

    trace_calls: list[str] = []
    original_requests_request = requests.sessions.Session.request
    original_httpx_send = httpx.AsyncClient.send

    def checked_requests_request(self, method, url, *args, **kwargs):
        if "langchain.com" in str(url):
            trace_calls.append(str(url))
            raise AssertionError(f"unexpected outbound trace request: {url}")
        return original_requests_request(self, method, url, *args, **kwargs)

    async def checked_httpx_send(self, request, *args, **kwargs):
        if "langchain.com" in str(request.url):
            trace_calls.append(str(request.url))
            raise AssertionError(f"unexpected outbound trace request: {request.url}")
        return await original_httpx_send(self, request, *args, **kwargs)

    with (
        patch.object(requests.sessions.Session, "request", checked_requests_request),
        patch.object(httpx.AsyncClient, "send", checked_httpx_send),
    ):
        output_path = tmp_path / "baseline.json"
        result = asyncio.run(runner._execute_all(output_path))
        asyncio.run(asyncio.sleep(0.1))

    assert result["complete"] is True
    assert trace_calls == []
    assert all(os.environ.get(name) == "false" for name in runner._TRACING_ENV_VARS)


@pytest.mark.asyncio
async def test_baseline_refuses_remote_database_before_initialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        runner,
        "get_settings",
        lambda: SimpleNamespace(
            llm_provider="ollama",
            llm_max_output_tokens=2048,
            tiger_database_url="postgresql+asyncpg://user:password@db.example.com:5432/reviews",
        ),
    )

    async def database_must_not_initialize() -> None:
        raise AssertionError("remote database initialization was reached")

    monkeypatch.setattr(runner, "_initialize_local_database", database_must_not_initialize)

    with pytest.raises(RuntimeError, match="loopback PostgreSQL"):
        await runner._execute_all(tmp_path / "baseline.json")

    assert not (tmp_path / "baseline.json").exists()


@pytest.mark.asyncio
async def test_budget_exhaustion_mid_run_aborts_without_writing_partial_baseline(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "baseline.json"
    records: dict[str, list[JsonRecord]] = {
        "attacks": [{"id": "RT-001"}, {"id": "RT-002"}],
        "benign": [{"id": "BN-001"}],
    }
    completed: list[str] = []

    async def execute(corpus: str, record: dict[str, object]) -> dict[str, object]:
        case_id = str(record["id"])
        if case_id == "RT-002":
            raise BudgetExceededError("test budget exhausted")
        completed.append(case_id)
        return {"id": case_id, "corpus": corpus}

    with pytest.raises(BaselineRunAborted, match="no result file written") as caught:
        await run_complete_baseline(
            records,
            execute,
            output_path,
            provider="ollama",
            model="test",
            execution_config={"timeout_s": 90},
        )

    assert completed == ["RT-001"]
    assert caught.value.corpus_counts["attacks"].attempted == 2
    assert caught.value.corpus_counts["attacks"].completed == 1
    assert caught.value.corpus_counts["attacks"].skipped == 0
    assert caught.value.corpus_counts["benign"].attempted == 0
    assert caught.value.corpus_counts["benign"].completed == 0
    assert caught.value.corpus_counts["benign"].skipped == 1
    assert not output_path.exists()


@pytest.mark.asyncio
async def test_complete_run_writes_results_and_balanced_corpus_counts(tmp_path: Path) -> None:
    output_path = tmp_path / "baseline.json"
    corpora: dict[str, list[JsonRecord]] = {
        "attacks": [{"id": "RT-001"}],
        "benign": [{"id": "BN-001"}],
    }

    async def execute(corpus: str, record: dict[str, object]) -> dict[str, object]:
        outcome = "REVIEW_FAILED" if record["id"] == "RT-001" else "ISOLATED"
        return {"id": record["id"], "corpus": corpus, "outcome": outcome}

    result = await run_complete_baseline(
        corpora,
        execute,
        output_path,
        provider="ollama",
        model="qwen2.5:7b-instruct",
        execution_config={
            "provider": "ollama",
            "model": "qwen2.5:7b-instruct",
            "quantization": "Q4_K_M",
            "production_timeout_s": 90.0,
            "max_output_tokens": 2048,
        },
    )

    assert result["complete"] is True
    assert json.loads(output_path.read_text()) == result
    counts = result["corpora"]
    assert isinstance(counts, dict)
    attack_counts = counts["attacks"]
    benign_counts = counts["benign"]
    assert isinstance(attack_counts, dict) and attack_counts["review_failed"] == 1
    assert isinstance(benign_counts, dict) and benign_counts["review_failed"] == 0
    for count in counts.values():
        assert isinstance(count, dict)
        assert count["attempted"] + count["skipped"] == count["expected"]
        assert count["completed"] + count["skipped"] == count["expected"]


def test_before_after_comparison_rejects_execution_config_mismatch() -> None:
    common = {
        "provider": "ollama",
        "model": "qwen2.5:7b-instruct",
        "execution_config_fingerprint": "same",
    }
    assert_comparable_baselines(common, common)
    with pytest.raises(ValueError, match="comparison refused"):
        assert_comparable_baselines(
            common,
            {
                **common,
                "execution_config_fingerprint": "different",
            },
        )


def test_persistent_influence_requires_control_finding_and_poison_retrieval() -> None:
    target = "sql-injection:backend/events/search.py"
    assert evaluate_persistent_influence(
        target,
        [target],
        [],
        poisoned_document_retrieved=True,
    )
    assert not evaluate_persistent_influence(
        target,
        [],
        [],
        poisoned_document_retrieved=True,
    )
    assert not evaluate_persistent_influence(
        target,
        [target],
        [],
        poisoned_document_retrieved=False,
    )
    assert not evaluate_persistent_influence(
        target,
        [target],
        [target],
        poisoned_document_retrieved=True,
    )


def test_baseline_subset_merge_is_atomic_and_recalculates_failures(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    path.write_text(
        json.dumps(
            {
                "complete": True,
                "corpora": {
                    "attacks": {
                        "expected": 2,
                        "attempted": 2,
                        "completed": 2,
                        "skipped": 0,
                        "review_failed": 1,
                    }
                },
                "results": [
                    {"id": "RT-001", "corpus": "attacks", "outcome": "REVIEW_FAILED"},
                    {"id": "RT-002", "corpus": "attacks", "outcome": "ISOLATED"},
                ],
            }
        )
    )

    updated = merge_baseline_records(
        path,
        [{"id": "RT-001", "corpus": "attacks", "outcome": "SUCCEEDED"}],
    )

    assert updated["results"][0]["outcome"] == "SUCCEEDED"
    assert updated["corpora"]["attacks"]["review_failed"] == 0
    with pytest.raises(ValueError, match="unknown record"):
        merge_baseline_records(
            path,
            [{"id": "RT-999", "corpus": "attacks", "outcome": "ISOLATED"}],
        )
    assert json.loads(path.read_text()) == updated
