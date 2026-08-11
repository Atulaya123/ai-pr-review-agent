"""Offline Agent Eval harness: retrieval metrics (Recall@k, MRR) against a
hand-built test set, plus generation metrics (Faithfulness, Relevance) via
LLM-as-judge over fixture findings.

Distinct from backend/hitl/gate.py's confidence gate, which only checks what
a specialist self-reports. This checks specialist output against the source
material — the retrieved context, and the diff itself.

Uses the real LLM_PROVIDER from your environment (defaults to the mock
client, same as the rest of the test suite — zero cost, zero network).

Retrieval metrics need code_chunks already ingested:
    PYTHONPATH=. python -m scripts.ingest_docs
Then:
    PYTHONPATH=. python -m scripts.run_eval
"""

import asyncio

from backend.agents.base_agent import resolve_model_for_provider
from backend.core.config import get_settings
from backend.evaluation.dataset import GENERATION_TEST_SET, RETRIEVAL_TEST_SET
from backend.evaluation.generation_metrics import pass_rate, score_faithfulness, score_relevance
from backend.evaluation.retrieval_metrics import mean_reciprocal_rank, recall_at_k
from backend.memory.embedder import embed_text
from backend.memory.tiger_client import query_similar_chunk_symbols
from backend.tools.llm_client import get_llm_client


async def run_retrieval_eval(settings) -> None:
    print("=== Retrieval metrics (Recall@3, MRR) ===")
    mrr_inputs: list[tuple[list[str], set[str]]] = []
    for case in RETRIEVAL_TEST_SET:
        embedding = await embed_text(case.query, settings)
        retrieved = await query_similar_chunk_symbols(case.repo, embedding, top_k=5)
        recall = recall_at_k(retrieved, case.relevant_symbols, k=3)
        mrr_inputs.append((retrieved, case.relevant_symbols))
        top1 = retrieved[0] if retrieved else "(nothing retrieved — has ingest_docs run?)"
        print(f"  recall@3={recall:.2f}  top1={top1:<40}  {case.query[:55]!r}")
    print(f"  MRR: {mean_reciprocal_rank(mrr_inputs):.3f}\n")


async def run_generation_eval(settings) -> None:
    print("=== Generation metrics (LLM-as-judge: Faithfulness, Relevance) ===")
    llm = get_llm_client(settings)
    model = resolve_model_for_provider(settings.llm_provider)

    faithfulness_verdicts: list[bool] = []
    relevance_verdicts: list[bool] = []
    for case in GENERATION_TEST_SET:
        supported, f_reason = await score_faithfulness(case.finding, case.context, llm, model=model)
        relevant, r_reason = await score_relevance(case.finding, case.diff_text, llm, model=model)
        faithfulness_verdicts.append(supported)
        relevance_verdicts.append(relevant)
        f_mark = "OK" if supported == case.expect_supported else "UNEXPECTED"
        r_mark = "OK" if relevant == case.expect_relevant else "UNEXPECTED"
        print(f"  [{case.name}] supported={supported} ({f_mark})  relevant={relevant} ({r_mark})")

    print(f"\n  Faithfulness rate: {pass_rate(faithfulness_verdicts):.2f}")
    print(f"  Relevance rate:    {pass_rate(relevance_verdicts):.2f}")


async def main() -> None:
    settings = get_settings()
    await run_retrieval_eval(settings)
    await run_generation_eval(settings)


if __name__ == "__main__":
    asyncio.run(main())
