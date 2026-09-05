from dataclasses import dataclass

from sqlalchemy import text

from backend.database.session import get_sessionmaker


def to_vector_literal(embedding: list[float]) -> str:
    return "[" + ",".join(str(v) for v in embedding) + "]"


@dataclass(frozen=True)
class ChunkRecord:
    path: str
    symbol: str
    chunk_index: int
    content: str
    embedding: list[float]


async def fetch_existing_hashes(repo: str) -> dict[tuple[str, int], str]:
    """(path, chunk_index) -> content_hash for every chunk currently stored for
    this repo. Used by the Airflow embed_batch task to skip re-embedding chunks
    whose content hasn't changed since the last run — see
    scripts/migrations/2026-09-content-hash.sql for why content_hash exists."""
    async with get_sessionmaker()() as session:
        result = await session.execute(
            text("SELECT path, chunk_index, content_hash FROM code_chunks WHERE repo = :repo"),
            {"repo": repo},
        )
        return {(row.path, row.chunk_index): row.content_hash for row in result}


async def upsert_chunks(repo: str, chunks: list[ChunkRecord]) -> int:
    """Idempotent upsert keyed on the (repo, path, chunk_index) unique index —
    re-running with unchanged content updates rows in place rather than
    duplicating them. content_hash is derived automatically (generated
    column), never written directly."""
    async with get_sessionmaker()() as session:
        for chunk in chunks:
            await session.execute(
                text(
                    """
                    INSERT INTO code_chunks (repo, path, symbol, chunk_index, content, embedding)
                    VALUES (:repo, :path, :symbol, :chunk_index, :content, CAST(:emb AS vector))
                    ON CONFLICT (repo, path, chunk_index) DO UPDATE
                        SET content = EXCLUDED.content,
                            symbol = EXCLUDED.symbol,
                            embedding = EXCLUDED.embedding,
                            updated_at = now()
                    """
                ),
                {
                    "repo": repo,
                    "path": chunk.path,
                    "symbol": chunk.symbol,
                    "chunk_index": chunk.chunk_index,
                    "content": chunk.content,
                    "emb": to_vector_literal(chunk.embedding),
                },
            )
        await session.commit()
        return len(chunks)


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
