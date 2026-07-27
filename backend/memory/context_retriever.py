from backend.models.review import ReviewRequest


async def get_retrieved_context(request: ReviewRequest) -> str:
    """Hybrid DiskANN + FTS retrieval over code_chunks (Part II / L4 grounding).

    STUB for M1: returns no context, so specialists reason over the diff alone.
    M2 (`Live integrations` milestone) replaces this with a real query against
    Tiger Cloud's code_chunks table — see PLAN.md M2 and pr-review-agent.html
    section 3.5 "The Retrieval Path".
    """
    return ""
