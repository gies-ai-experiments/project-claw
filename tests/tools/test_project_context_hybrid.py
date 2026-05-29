"""Hybrid (FTS + pgvector) search tests for project_context_search (Task 15)."""
from __future__ import annotations

import pytest

from nanobot.agent.tools.project_context import search_project_context
from nanobot.store.embeddings import to_pgvector
from nanobot.store.migrations import apply_migrations

DIM = 1536


def _vec(*nonzero: int) -> list[float]:
    v = [0.0] * DIM
    for i in nonzero:
        v[i] = 1.0
    return v


class FakeEmbedder:
    def __init__(self, vec: list[float]):
        self._vec = vec

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vec for _ in texts]


async def _insert_fact(conn, subject, body, vec):
    await conn.execute(
        """
        INSERT INTO project_facts
          (project_id, kind, subject, body, distiller_version,
           source_message_ids, embedding)
        VALUES ('mf','decision',$1,$2,'v1',ARRAY[1],$3::vector)
        """,
        subject,
        body,
        to_pgvector(vec),
    )


@pytest.mark.asyncio
async def test_hybrid_surfaces_keyword_and_semantic_matches(pg_schema):
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)
    query_vec = _vec(0)
    # semantic match: same vector as the query, but no shared keyword
    await _insert_fact(conn, "login redesign", "We reworked the sign-in flow.", query_vec)
    # keyword match: shares "auth", different vector
    await _insert_fact(conn, "auth approach", "We chose auth option B.", _vec(5))

    out = await search_project_context(
        conn, project_id="mf", query="auth", limit=5, embedder=FakeEmbedder(query_vec)
    )
    subjects = {r["subject"] for r in out}
    assert "auth approach" in subjects  # full-text hit
    assert "login redesign" in subjects  # semantic hit despite no keyword overlap


@pytest.mark.asyncio
async def test_hybrid_excludes_unrelated_facts(pg_schema):
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)
    query_vec = _vec(0)
    await _insert_fact(conn, "auth approach", "We chose auth option B.", _vec(0))
    # unrelated: distant vector AND no keyword overlap -> excluded
    await _insert_fact(conn, "lunch plans", "Tacos on friday.", _vec(900))

    out = await search_project_context(
        conn, project_id="mf", query="auth", limit=5, embedder=FakeEmbedder(query_vec)
    )
    subjects = {r["subject"] for r in out}
    assert "auth approach" in subjects
    assert "lunch plans" not in subjects
