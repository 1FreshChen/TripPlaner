from __future__ import annotations

from typing import Any, Protocol

from openai import AsyncOpenAI

from app.config import VECTOR_MEMORY_DIMENSIONS, Settings


class EmbeddingService(Protocol):
    """Minimal embedding contract used by the vector-memory store."""

    model: str

    async def embed(self, text: str) -> list[float]: ...


class OpenAIEmbeddingService:
    """Generate fixed-size embeddings with an OpenAI-compatible endpoint."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout_seconds: float,
        client: Any | None = None,
    ) -> None:
        self.model = model
        self._client = client or AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout_seconds,
        )

    async def embed(self, text: str) -> list[float]:
        normalized = " ".join(text.split())
        if not normalized:
            raise ValueError("embedding input cannot be empty")
        response = await self._client.embeddings.create(
            model=self.model,
            input=normalized,
            dimensions=VECTOR_MEMORY_DIMENSIONS,
        )
        vector = [float(value) for value in response.data[0].embedding]
        if len(vector) != VECTOR_MEMORY_DIMENSIONS:
            raise ValueError(
                f"embedding dimension mismatch: expected {VECTOR_MEMORY_DIMENSIONS}, got {len(vector)}"
            )
        return vector


def build_embedding_service(settings: Settings) -> EmbeddingService | None:
    """Build the configured embedding client, or disable vector memory without a key."""

    if not settings.vector_memory_enabled:
        return None
    api_key = settings.embedding_api_key or settings.llm_api_key
    if not api_key:
        return None
    return OpenAIEmbeddingService(
        api_key=api_key,
        base_url=settings.embedding_base_url or settings.llm_base_url,
        model=settings.embedding_model,
        timeout_seconds=settings.embedding_timeout_seconds,
    )
