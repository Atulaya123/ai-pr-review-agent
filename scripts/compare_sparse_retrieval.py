"""Local PostgreSQL retrieval comparison with fixed synthetic vector ordering and recorded LLM outputs."""

import asyncio
import json
import subprocess
from pathlib import Path
from uuid import UUID

import mlflow

from backend.core.config import Settings
from backend.database.cost_ledger import CallPurpose
from backend.evaluation.experiments import run_configuration_comparison
from backend.evaluation.replay import (
    ReviewReplayFixture,
    _RecordedLLMClient,
    load_replay_fixture,
)
from backend.memory.sparse_query import build_sparse_query
from backend.tools.llm_client import LLMResponse
from scripts.ingest_docs import CHUNKS


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _retrieve(document: str, mode: str) -> dict[str, object]:
    query = build_sparse_query(document) if mode == "bounded_or" else document
    values = ",".join(
        f"({index + 1},{_literal(symbol)},{_literal(content)})"
        for index, (_, symbol, content) in enumerate(CHUNKS)
    )
    sql = f"""
    WITH corpus(id,symbol,content) AS (VALUES {values}),
    query AS (SELECT websearch_to_tsquery('english',{_literal(query)}) AS q),
    sparse AS (
        SELECT id, row_number() OVER (
            ORDER BY ts_rank(to_tsvector('english',content),q) DESC,id
        ) AS rank
        FROM corpus CROSS JOIN query WHERE to_tsvector('english',content) @@ q
    ), fused AS (
        SELECT c.*, 1.0/(60+c.id) + COALESCE(1.0/(60+s.rank),0) AS score
        FROM corpus c LEFT JOIN sparse s USING(id)
        ORDER BY score DESC,c.id LIMIT 5
    )
    SELECT json_build_object(
        'sparse_count',(SELECT count(*) FROM sparse),
        'sparse_symbols',(SELECT COALESCE(json_agg(symbol),'[]'::json)
            FROM corpus WHERE id IN (SELECT id FROM sparse)),
        'context',(SELECT string_agg(content,E'\\n\\n' ORDER BY score DESC,id) FROM fused)
    );
    """
    result = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "postgres",
            "-d",
            "postgres",
            "-At",
            "-v",
            "ON_ERROR_STOP=1",
        ],
        input=sql,
        text=True,
        capture_output=True,
        check=True,
    )
    payload: dict[str, object] = json.loads(result.stdout)
    return payload


async def main() -> None:
    fixture_dir = Path("backend/tests/fixtures/replays")
    fixtures = [load_replay_fixture(path) for path in sorted(fixture_dir.glob("*.json"))]
    observations: list[dict[str, object]] = []

    async def retrieve(fixture: ReviewReplayFixture, settings: Settings) -> str:
        document = (
            "\n".join(f"--- {file.path} ---\n{file.patch}" for file in fixture.request.files)
            if settings.sparse_query_mode == "bounded_or"
            else "\n".join(file.patch for file in fixture.request.files)
        )
        payload = await asyncio.to_thread(_retrieve, document, settings.sparse_query_mode)
        observations.append(
            {
                "fixture": fixture.name,
                "mode": settings.sparse_query_mode,
                "sparse_count": payload["sparse_count"],
                "sparse_symbols": payload["sparse_symbols"],
            }
        )
        return str(payload["context"] or "")

    responses = {
        agent: response
        for fixture in fixtures
        for agent, response in fixture.model_responses.items()
    }

    # Dispatch each recorded response by its exact fixture review id.
    class RecordedClient(_RecordedLLMClient):
        async def complete_json(
            self,
            *,
            system: str,
            user: str,
            model: str,
            max_output_tokens: int = 2048,
            purpose: CallPurpose = "agent",
            review_id: UUID | None = None,
            agent: str | None = None,
        ) -> LLMResponse:
            fixture = next(f for f in fixtures if f.request.review_id == review_id)
            client = _RecordedLLMClient(
                {
                    specialist: response.model_copy(update={"model": model})
                    for specialist, response in fixture.model_responses.items()
                }
            )
            return await client.complete_json(
                system=system,
                user=user,
                model=model,
                max_output_tokens=max_output_tokens,
                purpose=purpose,
                review_id=review_id,
                agent=agent,
            )

    tracking_uri = "sqlite:///" + str(Path(".mlflow/sparse-comparison.db").resolve())
    Path(".mlflow").mkdir(exist_ok=True)
    comparison = await run_configuration_comparison(
        fixtures,
        settings=Settings.model_construct(llm_provider="mock", sparse_query_mode="whole_document"),
        candidate_retrieval_mode="bounded_or",
        retrieval_runner=retrieve,
        client_factory=lambda settings: RecordedClient(responses),
        tracking_uri=tracking_uri,
        experiment_name="sparse-query-local-replay",
    )
    with mlflow.start_run(run_name="retrieval-evidence"):
        mlflow.log_dict({"observations": observations}, "retrieval-observations.json")
        mlflow.log_dict(
            {
                "corpus": CHUNKS,
                "vector_ordering": "Synthetic fixed CHUNKS ordering; no embedding or production vectors.",
                "model_responses": "Fixed replay responses; quality deltas cannot show live-model benefit.",
                "latency": "Local end-to-end replay including psql process startup; single ordered run, no performance claim.",
                "cost": "Provider-free replay cost, not an estimate of live provider cost.",
            },
            "provenance.json",
        )
        for mode in ("whole_document", "bounded_or"):
            rows = [row for row in observations if row["mode"] == mode]
            counts = [int(str(row["sparse_count"])) for row in rows]
            mlflow.log_metric(f"{mode}_mean_sparse_candidates", sum(counts) / len(counts))
    report = {"comparison": comparison.model_dump(mode="json"), "retrieval": observations}
    Path(".mlflow/sparse-comparison.json").write_text(json.dumps(report, indent=2))
    print(json.dumps({"retrieval": observations, "deltas": comparison.aggregate_deltas}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
