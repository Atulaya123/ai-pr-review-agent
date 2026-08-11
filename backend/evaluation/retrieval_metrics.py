"""Retrieval-quality metrics for the RAG lane (backend/memory/).

Pure functions, no I/O — the DB-backed side (embedding a query, calling
query_similar_chunk_symbols) lives in scripts/run_eval.py, which is what
actually exercises pgvector/DiskANN. Kept separate so these stay unit-testable
without a live database, same reasoning as the pure _langsmith_env_vars()
split (docs/ARCHITECTURE_DECISIONS.md #10).
"""


def recall_at_k(retrieved: list[str], relevant: set[str], k: int) -> float:
    """Fraction of the relevant set that shows up in the top-k retrieved items."""
    if not relevant:
        return 1.0
    top_k = set(retrieved[:k])
    return len(top_k & relevant) / len(relevant)


def reciprocal_rank(retrieved: list[str], relevant: set[str]) -> float:
    """1 / rank of the first relevant item retrieved; 0.0 if none appear at all."""
    for rank, item in enumerate(retrieved, start=1):
        if item in relevant:
            return 1.0 / rank
    return 0.0


def mean_reciprocal_rank(results: list[tuple[list[str], set[str]]]) -> float:
    """MRR across a test set of (retrieved, relevant) pairs."""
    if not results:
        return 0.0
    return sum(reciprocal_rank(retrieved, relevant) for retrieved, relevant in results) / len(results)
