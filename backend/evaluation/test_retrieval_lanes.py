from collections.abc import Iterator
import logging
from types import SimpleNamespace

import pytest
from sqlalchemy.sql.elements import TextClause

from backend.memory import tiger_client


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

    async def __aenter__(self) -> "_Session":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def execute(self, statement: TextClause, params: dict[str, object]) -> _Result:
        del params
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
