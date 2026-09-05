import httpx

from backend.core.config import Settings, get_settings


async def embed_text(text: str, settings: Settings | None = None) -> list[float]:
    """Embed text for the vector lane. Ollama is the free local default;
    EMBEDDING_PROVIDER=gemini or =openai switch to a hosted call instead
    (matching embedding_dims to whichever model is configured — see
    core/config.py). Both hosted providers truncate their native output down
    to embedding_dims (768) — Gemini via output_dimensionality, OpenAI via
    `dimensions` — so code_chunks.embedding stays VECTOR(768) regardless of
    provider; switching still needs re-ingestion so stored vectors match
    query-time vectors (they live in different embedding spaces per model)."""
    settings = settings or get_settings()

    if settings.embedding_provider == "gemini" and settings.gemini_api_key:
        from google import genai
        from google.genai import types

        gemini_client = genai.Client(api_key=settings.gemini_api_key)
        gemini_resp = await gemini_client.aio.models.embed_content(
            model=settings.embedding_model,
            contents=text,
            config=types.EmbedContentConfig(output_dimensionality=settings.embedding_dims),
        )
        if not gemini_resp.embeddings or gemini_resp.embeddings[0].values is None:
            raise RuntimeError("Gemini embed_content returned no embedding values")
        return gemini_resp.embeddings[0].values

    if settings.embedding_provider == "openai" and settings.openai_api_key:
        from openai import AsyncOpenAI

        openai_client = AsyncOpenAI(api_key=settings.openai_api_key)
        openai_resp = await openai_client.embeddings.create(
            model=settings.embedding_model,
            input=text,
            dimensions=settings.embedding_dims,
        )
        return openai_resp.data[0].embedding

    async with httpx.AsyncClient(timeout=30.0) as http_client:
        http_resp = await http_client.post(
            "http://localhost:11434/api/embed",
            json={"model": settings.embedding_model, "input": text},
        )
        http_resp.raise_for_status()
        return http_resp.json()["embeddings"][0]
