from collections.abc import Iterator
import logging
import subprocess
import sys
import json
from types import SimpleNamespace

import pytest
from sqlalchemy.sql.elements import TextClause

from backend.memory import tiger_client
from backend.core.config import Settings


class _Result:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self._rows = rows

    def __iter__(self) -> Iterator[SimpleNamespace]:
        return iter(self._rows)


class _Session:
    def __init__(
        self,
        vector_rows: list[SimpleNamespace],
        fulltext_rows: list[SimpleNamespace],
    ) -> None:
        self._vector_rows = vector_rows
        self._fulltext_rows = fulltext_rows
        self.queries: list[dict[str, object]] = []

    async def __aenter__(self) -> "_Session":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def execute(self, statement: TextClause, params: dict[str, object]) -> _Result:
        self.queries.append(params)
        rows = self._fulltext_rows if "content_tsv" in str(statement) else self._vector_rows
        return _Result(rows)


def _row(identifier: int, path: str) -> SimpleNamespace:
    return SimpleNamespace(id=identifier, path=path, content=f"chunk {identifier}")


@pytest.mark.asyncio
async def test_hybrid_search_logs_vector_only_when_fulltext_lane_is_empty(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="backend.memory.tiger_client")
    session = _Session([_row(1, "vector.py")], [])
    monkeypatch.setattr(tiger_client, "get_sessionmaker", lambda: lambda: session)

    results = await tiger_client.hybrid_search_chunks("owner/repo", [0.1, 0.2], "rare_identifier")

    assert results == ["[vector.py]\nchunk 1"]
    assert (
        "retrieval_lane_counts query_type=content mode=vector_only "
        "vector_count=1 fulltext_count=0 overlap_count=0 fused_count=1"
    ) in caplog.text


@pytest.mark.asyncio
async def test_hybrid_search_logs_both_lanes_before_fusion(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="backend.memory.tiger_client")
    session = _Session(
        [_row(1, "vector.py"), _row(2, "shared.py")],
        [_row(2, "shared.py"), _row(3, "fulltext.py")],
    )
    monkeypatch.setattr(tiger_client, "get_sessionmaker", lambda: lambda: session)

    results = await tiger_client.hybrid_search_chunks("owner/repo", [0.1, 0.2], "rare_identifier")

    assert len(results) == 3
    assert (
        "retrieval_lane_counts query_type=content mode=hybrid "
        "vector_count=2 fulltext_count=2 overlap_count=1 fused_count=3"
    ) in caplog.text


def test_render_worker_configuration_emits_retrieval_info_logs() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from logging.config import dictConfig; "
            "from backend.observability.logging import WORKER_LOG_CONFIG; "
            "from backend.memory.tiger_client import _log_retrieval_lanes; "
            "dictConfig(WORKER_LOG_CONFIG); "
            "_log_retrieval_lanes(query_type='content', vector_ids={'v'}, "
            "fulltext_ids=set(), final_count=1)",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    line = json.loads(result.stderr.strip())
    assert line["event"] == "retrieval.lanes"
    assert line["mode"] == "vector_only"
    assert line["vector_count"] == 1
    assert line["fulltext_count"] == 0


@pytest.mark.parametrize("symbols", [False, True])
async def test_both_sparse_search_paths_use_bounded_or_query(
    monkeypatch: pytest.MonkeyPatch, symbols: bool
) -> None:
    session = _Session([], [])
    monkeypatch.setattr(tiger_client, "get_sessionmaker", lambda: lambda: session)
    monkeypatch.setattr(
        tiger_client,
        "get_settings",
        lambda: Settings.model_construct(sparse_query_mode="bounded_or"),
    )
    search = (
        tiger_client.hybrid_search_chunk_symbols if symbols else tiger_client.hybrid_search_chunks
    )
    await search("owner/repo", [0.1], "retrieve_context get_findings")
    assert session.queries[0]["emb"] == "[0.1]"
    assert session.queries[1]["query"] == '"get_findings" OR "retrieve_context"'
