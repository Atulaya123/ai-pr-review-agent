from sqlalchemy import text

from backend.database.session import get_sessionmaker


def to_vector_literal(embedding: list[float]) -> str:
    return "[" + ",".join(str(v) for v in embedding) + "]"


async def query_similar_chunks(repo: str, embedding: list[float], top_k: int = 5) -> list[str]:
    """Cosine-similarity search over code_chunks (the vector lane) via pgvector's
    <=> operator and the DiskANN index. Returns the raw content of the top-k
    nearest chunks for this repo, closest first."""
    async with get_sessionmaker()() as session:
        result = await session.execute(
            text(
                """
                SELECT content, path
                FROM code_chunks
                WHERE repo = :repo
                ORDER BY embedding <=> CAST(:emb AS vector)
                LIMIT :k
                """
            ),
            {"repo": repo, "emb": to_vector_literal(embedding), "k": top_k},
        )
        return [f"[{row.path}]\n{row.content}" for row in result]


async def query_similar_chunk_symbols(repo: str, embedding: list[float], top_k: int = 5) -> list[str]:
    """Same ranked search as query_similar_chunks, but returns chunk symbols
    instead of content — used by the eval harness (backend/evaluation/) to
    check *which* chunk was retrieved against a test set, not to render a
    prompt."""
    async with get_sessionmaker()() as session:
        result = await session.execute(
            text(
                """
                SELECT symbol
                FROM code_chunks
                WHERE repo = :repo
                ORDER BY embedding <=> CAST(:emb AS vector)
                LIMIT :k
                """
            ),
            {"repo": repo, "emb": to_vector_literal(embedding), "k": top_k},
        )
        return [row.symbol for row in result]
