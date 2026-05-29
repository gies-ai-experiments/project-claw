"""Embedding provider for L2/L3 semantic search."""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Embedder(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]:
        ...


class OpenAIEmbedder:
    """Embeds text via the OpenAI embeddings API (text-embedding-3-small = 1536d)."""

    def __init__(self, client, model: str = "text-embedding-3-small"):
        self._client = client
        self._model = model

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        r = await self._client.embeddings.create(model=self._model, input=texts)
        return [d.embedding for d in r.data]


def to_pgvector(vec: list[float]) -> str:
    """Render a float vector as pgvector's text literal ('[a,b,c]')."""
    return "[" + ",".join(str(float(x)) for x in vec) + "]"
