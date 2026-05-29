"""`project_context_search` — query distilled per-project facts (L2).

The FTS query lives here as a plain async function so it is unit-testable
without the tool runtime. ``ProjectContextSearchTool`` exposes it to the agent,
injecting ``project_id`` from runtime context (never from model input) — the
same scoping invariant as the rest of projectclaw.
"""
from __future__ import annotations

from typing import Any, Optional

import asyncpg

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.context import RequestContext


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


def _render_results(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "No matching project context found."
    lines = []
    for r in rows:
        lines.append(f"- ({r['kind']}) {r['subject']}: {r['body']}")
    return "\n".join(lines)


@tool_parameters(
    {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What to look up in this project's distilled history.",
            },
            "kind": {
                "type": ["string", "null"],
                "enum": ["decision", "action", "fact", "open_question", "role", None],
                "description": "Optional filter by fact kind.",
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 25},
        },
        "required": ["query"],
    }
)
class ProjectContextSearchTool(Tool):
    """Search the current project's distilled facts (L2).

    ``project_id`` is taken from the per-turn runtime context, NOT from the
    model — the model only chooses the query/kind/limit. Without a resolved
    project the tool returns a hint rather than searching across projects.
    """

    name = "project_context_search"
    description = (
        "Search this project's remembered decisions, actions, facts, open questions "
        "and roles (distilled from past conversations). Scoped to the current project."
    )
    read_only = True

    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool
        self._ctx: RequestContext | None = None

    def set_context(self, ctx: RequestContext) -> None:
        self._ctx = ctx

    def _project_id(self) -> Optional[str]:
        meta = self._ctx.metadata if self._ctx else {}
        pid = meta.get("project_id")
        if pid:
            return pid
        project = meta.get("project")
        if isinstance(project, dict):
            return project.get("name")
        return None

    async def execute(self, **kwargs: Any) -> str:
        project_id = self._project_id()
        if not project_id:
            return (
                "No project is resolved for this thread, so project context is "
                "unavailable. Ask the user which project this refers to."
            )
        rows = await search_project_context(
            self._pool,
            project_id=project_id,
            query=kwargs["query"],
            kind=kwargs.get("kind"),
            limit=int(kwargs.get("limit", 8)),
        )
        return _render_results(rows)
