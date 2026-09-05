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

from backend.core.config import get_settings
from backend.evaluation.runner import compute_generation_metrics, compute_retrieval_metrics


async def run_retrieval_eval(settings) -> None:
    print("=== Retrieval metrics (Recall@3, MRR) ===")
    metrics = await compute_retrieval_metrics(settings)
    for case in metrics.cases:
        top1 = case.retrieved[0] if case.retrieved else "(nothing retrieved — has ingest_docs run?)"
        print(f"  recall@3={case.recall_at_3:.2f}  top1={top1:<40}  {case.query[:55]!r}")
    print(f"  MRR: {metrics.mrr:.3f}\n")


async def run_generation_eval(settings) -> None:
    print("=== Generation metrics (LLM-as-judge: Faithfulness, Relevance) ===")
    metrics = await compute_generation_metrics(settings)
    for case in metrics.cases:
        f_mark = "OK" if case.supported == case.expected_supported else "UNEXPECTED"
        r_mark = "OK" if case.relevant == case.expected_relevant else "UNEXPECTED"
        print(f"  [{case.name}] supported={case.supported} ({f_mark})  relevant={case.relevant} ({r_mark})")

    print(f"\n  Faithfulness rate: {metrics.faithfulness_rate:.2f}")
    print(f"  Relevance rate:    {metrics.relevance_rate:.2f}")


async def main() -> None:
    settings = get_settings()
    await run_retrieval_eval(settings)
    await run_generation_eval(settings)


if __name__ == "__main__":
    asyncio.run(main())
