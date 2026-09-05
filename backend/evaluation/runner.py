"""Programmatic entry points for the Agent Eval harness — return metrics as
data instead of printing them, so scripts/run_eval.py (human-readable output)
and the Airflow eval gate (airflow/dags/knowledge_base_refresh.py) stay backed
by the exact same computation instead of two copies that can drift apart.
"""

from dataclasses import dataclass

from backend.agents.base_agent import resolve_model_for_provider
from backend.core.config import Settings
from backend.evaluation.dataset import GENERATION_TEST_SET, RETRIEVAL_TEST_SET
from backend.evaluation.generation_metrics import pass_rate, score_faithfulness, score_relevance
from backend.evaluation.retrieval_metrics import mean_reciprocal_rank, recall_at_k
from backend.memory.embedder import embed_text
from backend.memory.tiger_client import query_similar_chunk_symbols
from backend.tools.llm_client import get_llm_client


@dataclass(frozen=True)
class RetrievalCaseResult:
    query: str
    retrieved: list[str]
    relevant: set[str]
    recall_at_3: float


@dataclass(frozen=True)
class RetrievalMetrics:
    cases: list[RetrievalCaseResult]
    mean_recall_at_3: float
    mrr: float


async def compute_retrieval_metrics(settings: Settings) -> RetrievalMetrics:
    cases: list[RetrievalCaseResult] = []
    mrr_inputs: list[tuple[list[str], set[str]]] = []
    for case in RETRIEVAL_TEST_SET:
        embedding = await embed_text(case.query, settings)
        retrieved = await query_similar_chunk_symbols(case.repo, embedding, top_k=5)
        recall = recall_at_k(retrieved, case.relevant_symbols, k=3)
        mrr_inputs.append((retrieved, case.relevant_symbols))
        cases.append(
            RetrievalCaseResult(
                query=case.query, retrieved=retrieved, relevant=case.relevant_symbols, recall_at_3=recall
            )
        )
    mean_recall = sum(c.recall_at_3 for c in cases) / len(cases) if cases else 1.0
    return RetrievalMetrics(cases=cases, mean_recall_at_3=mean_recall, mrr=mean_reciprocal_rank(mrr_inputs))


@dataclass(frozen=True)
class GenerationCaseResult:
    name: str
    supported: bool
    relevant: bool
    expected_supported: bool
    expected_relevant: bool


@dataclass(frozen=True)
class GenerationMetrics:
    cases: list[GenerationCaseResult]
    faithfulness_rate: float
    relevance_rate: float


async def compute_generation_metrics(settings: Settings) -> GenerationMetrics:
    llm = get_llm_client(settings)
    model = resolve_model_for_provider(settings.llm_provider)

    cases: list[GenerationCaseResult] = []
    faithfulness_verdicts: list[bool] = []
    relevance_verdicts: list[bool] = []
    for case in GENERATION_TEST_SET:
        supported, _ = await score_faithfulness(case.finding, case.context, llm, model=model)
        relevant, _ = await score_relevance(case.finding, case.diff_text, llm, model=model)
        faithfulness_verdicts.append(supported)
        relevance_verdicts.append(relevant)
        cases.append(
            GenerationCaseResult(
                name=case.name,
                supported=supported,
                relevant=relevant,
                expected_supported=case.expect_supported,
                expected_relevant=case.expect_relevant,
            )
        )
    return GenerationMetrics(
        cases=cases,
        faithfulness_rate=pass_rate(faithfulness_verdicts),
        relevance_rate=pass_rate(relevance_verdicts),
    )
