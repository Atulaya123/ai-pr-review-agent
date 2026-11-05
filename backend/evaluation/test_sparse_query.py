from backend.memory.sparse_query import TERM_LIMIT, build_sparse_query


def test_sparse_query_bounds_terms_and_neutralizes_boolean_source_syntax() -> None:
    query = build_sparse_query(
        '+def retrieve_context():\n+return "NOT secrets OR ignore instructions"\n'
        + " ".join(f"identifier_{index}" for index in range(100))
    )
    assert len(query.split(" OR ")) == TERM_LIMIT
    assert '"retrieve_context"' in query
    assert all(term.startswith('"') and term.endswith('"') for term in query.split(" OR "))
    assert " AND " not in query


def test_sparse_query_keeps_prose_and_handles_empty_input() -> None:
    assert '"reliability"' in build_sparse_query("+Outbound reliability timeout")
    assert build_sparse_query("123 ++ ...") == ""
