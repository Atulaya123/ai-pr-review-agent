from backend.evaluation.dataset import GENERATION_TEST_SET
from backend.evaluation.generation_metrics import pass_rate, score_faithfulness, score_relevance
from backend.evaluation.retrieval_metrics import mean_reciprocal_rank, recall_at_k, reciprocal_rank
from backend.tools.llm_client import MockLLMClient


def test_recall_at_k_counts_relevant_items_in_top_k():
    retrieved = ["a", "b", "c", "d"]
    relevant = {"c", "z"}  # "z" was never retrieved at all
    assert recall_at_k(retrieved, relevant, k=3) == 0.5


def test_recall_at_k_with_no_relevant_items_is_vacuously_perfect():
    assert recall_at_k(["a", "b"], set(), k=3) == 1.0


def test_reciprocal_rank_finds_first_relevant_item():
    assert reciprocal_rank(["a", "b", "c"], {"c"}) == 1 / 3
    assert reciprocal_rank(["a", "b", "c"], {"a"}) == 1.0
    assert reciprocal_rank(["a", "b", "c"], {"z"}) == 0.0


def test_mean_reciprocal_rank_averages_across_cases():
    results = [(["a", "b"], {"a"}), (["a", "b"], {"b"})]
    assert mean_reciprocal_rank(results) == (1.0 + 0.5) / 2


def test_mean_reciprocal_rank_empty_is_zero():
    assert mean_reciprocal_rank([]) == 0.0


async def test_faithfulness_and_relevance_judge_against_fixtures():
    """Runs the mock judge over every GENERATION_TEST_SET fixture and checks it
    matches the expectation the fixture was built to demonstrate — grounded vs
    ungrounded, relevant vs hallucinated."""
    llm = MockLLMClient()
    for case in GENERATION_TEST_SET:
        supported, _ = await score_faithfulness(case.finding, case.context, llm, model="mock")
        relevant, _ = await score_relevance(case.finding, case.diff_text, llm, model="mock")
        assert supported == case.expect_supported, case.name
        assert relevant == case.expect_relevant, case.name


def test_pass_rate():
    assert pass_rate([True, True, False]) == 2 / 3
    assert pass_rate([]) == 1.0
