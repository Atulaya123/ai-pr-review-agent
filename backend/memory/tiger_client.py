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
