"""Tests for the project_context_search FTS query (Task 14)."""
from __future__ import annotations

import pytest

from nanobot.agent.tools.context import RequestContext
from nanobot.agent.tools.project_context import (
    ProjectContextSearchTool,
    search_project_context,
)
from nanobot.store.migrations import apply_migrations


@pytest.mark.asyncio
async def test_search_returns_only_current_facts(pg_schema):
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)
    await conn.execute(
        """
        INSERT INTO project_facts
          (project_id, kind, subject, body, distiller_version, source_message_ids)
        VALUES
          ('mindforum','decision','auth approach',
           'We chose option B for the auth refactor.','v1',ARRAY[1,2]),
          ('mindforum','decision','old auth approach',
           'We chose option A for the auth refactor.','v1',ARRAY[3])
        """
    )
    await conn.execute(
        "UPDATE project_facts SET superseded_by=(SELECT id FROM project_facts "
        "WHERE subject='auth approach') WHERE subject='old auth approach'"
    )
    out = await search_project_context(
        conn, project_id="mindforum", query="auth refactor", limit=5
    )
    assert len(out) == 1
    assert out[0]["subject"] == "auth approach"


@pytest.mark.asyncio
async def test_search_is_scoped_to_project(pg_schema):
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)
    await conn.execute(
        """
        INSERT INTO project_facts
          (project_id, kind, subject, body, distiller_version, source_message_ids)
        VALUES
          ('mindforum','decision','auth A','We chose A.','v1',ARRAY[1]),
          ('projectclaw','decision','auth B','We chose B.','v1',ARRAY[2])
        """
    )
    out = await search_project_context(
        conn, project_id="mindforum", query="auth", limit=5
    )
    subjects = {r["subject"] for r in out}
    assert subjects == {"auth A"}


@pytest.mark.asyncio
async def test_search_filters_by_kind(pg_schema):
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)
    await conn.execute(
        """
        INSERT INTO project_facts
          (project_id, kind, subject, body, distiller_version, source_message_ids)
        VALUES
          ('mindforum','decision','auth decision','Chose auth B.','v1',ARRAY[1]),
          ('mindforum','open_question','auth question','Which auth lib?','v1',ARRAY[2])
        """
    )
    out = await search_project_context(
        conn, project_id="mindforum", query="auth", kind="open_question", limit=5
    )
    assert [r["kind"] for r in out] == ["open_question"]


@pytest.mark.asyncio
async def test_tool_uses_runtime_project_id_not_model_input(pg_schema):
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)
    await conn.execute(
        """
        INSERT INTO project_facts
          (project_id, kind, subject, body, distiller_version, source_message_ids)
        VALUES
          ('mindforum','decision','auth A','We chose A.','v1',ARRAY[1]),
          ('projectclaw','decision','auth B','We chose B.','v1',ARRAY[2])
        """
    )
    tool = ProjectContextSearchTool(pool=conn)
    tool.set_context(
        RequestContext(channel="slack", chat_id="C1", metadata={"project_id": "mindforum"})
    )
    out = await tool.execute(query="auth")
    assert "auth A" in out
    assert "auth B" not in out  # scoped to runtime project, not model-chosen


@pytest.mark.asyncio
async def test_tool_without_resolved_project_returns_hint(pg_schema):
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)
    tool = ProjectContextSearchTool(pool=conn)
    tool.set_context(RequestContext(channel="slack", chat_id="C1", metadata={}))
    out = await tool.execute(query="anything")
    assert "No project is resolved" in out
