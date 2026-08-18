import httpx

from backend.core.config import Settings, get_settings


async def embed_text(text: str, settings: Settings | None = None) -> list[float]:
    """Embed text for the vector lane. Ollama is the free local default; swap
    to an OpenAI embeddings call if EMBEDDING_PROVIDER=openai (matching
    embedding_dims to whichever model is configured — see core/config.py).
    `dimensions` truncates OpenAI's native output (1536/3072) down to
    embedding_dims (768) via Matryoshka truncation, so code_chunks.embedding
    stays VECTOR(768) regardless of provider — no schema change needed to
    switch providers, only re-ingestion so stored vectors match query vectors."""
    settings = settings or get_settings()

    if settings.embedding_provider == "openai" and settings.openai_api_key:
        from openai import AsyncOpenAI

        openai_client = AsyncOpenAI(api_key=settings.openai_api_key)
        embedding_resp = await openai_client.embeddings.create(
            model=settings.embedding_model,
            input=text,
            dimensions=settings.embedding_dims,
        )
        return embedding_resp.data[0].embedding

    async with httpx.AsyncClient(timeout=30.0) as http_client:
        http_resp = await http_client.post(
            "http://localhost:11434/api/embed",
            json={"model": settings.embedding_model, "input": text},
        )
        http_resp.raise_for_status()
        return http_resp.json()["embeddings"][0]
