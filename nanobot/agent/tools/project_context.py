"""`project_context_search` — query distilled per-project facts (L2).

The FTS query lives here as a plain async function so it is unit-testable
without the tool runtime. Tool registration (which injects ``project_id`` from
runtime context rather than model input) is wired separately.
"""
from __future__ import annotations

from typing import Any, Optional

import asyncpg


async def search_project_context(
    conn: asyncpg.Connection | asyncpg.Pool,
    project_id: str,
    query: str,
    kind: Optional[str] = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Full-text search current (non-superseded) facts for one project."""
    sql = """
        SELECT id, kind, subject, body, source_message_ids, created_at,
               ts_rank_cd(
                 to_tsvector('english', subject || ' ' || body),
                 plainto_tsquery('english', $2)) AS rank
        FROM project_facts
        WHERE project_id = $1
          AND superseded_by IS NULL
          AND ($3::text IS NULL OR kind = $3)
          AND to_tsvector('english', subject || ' ' || body)
              @@ plainto_tsquery('english', $2)
        ORDER BY rank DESC
        LIMIT $4
    """
    rows = await conn.fetch(sql, project_id, query, kind, limit)
    return [dict(r) for r in rows]
