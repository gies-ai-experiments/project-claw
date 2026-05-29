"""Tests for per-thread sticky project resolution."""
from __future__ import annotations

import pytest

from nanobot.channels.project_resolver import ProjectResolver, ResolveInput
from nanobot.store.migrations import apply_migrations


@pytest.mark.asyncio
async def test_explicit_prefix_resolves_and_locks(pg_schema):
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)
    await conn.execute(
        "INSERT INTO project_registry (project_id, allowed_channels) "
        "VALUES ('mindforum', ARRAY['C1']), ('projectclaw', ARRAY['C1'])"
    )
    resolver = ProjectResolver(conn=conn)
    out = await resolver.resolve(
        ResolveInput(
            channel_id="C1",
            thread_ts="t1",
            body="@projectclaw [mindforum] what shipped this week?",
        )
    )
    assert out.project_id == "mindforum"
    assert out.locked is True


@pytest.mark.asyncio
async def test_locked_thread_returns_cached_project(pg_schema):
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)
    await conn.execute(
        "INSERT INTO project_registry (project_id, allowed_channels) "
        "VALUES ('mindforum', ARRAY['C1'])"
    )
    await conn.execute(
        "INSERT INTO thread_project_lock (channel_id, thread_ts, project_id) "
        "VALUES ('C1', 't1', 'mindforum')"
    )
    resolver = ProjectResolver(conn=conn)
    out = await resolver.resolve(
        ResolveInput(channel_id="C1", thread_ts="t1", body="anything goes")
    )
    assert out.project_id == "mindforum"


@pytest.mark.asyncio
async def test_single_allowed_project_defaults_without_mention(pg_schema):
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)
    await conn.execute(
        "INSERT INTO project_registry (project_id, allowed_channels) "
        "VALUES ('projectclaw', ARRAY['C1'])"
    )
    resolver = ProjectResolver(conn=conn)
    out = await resolver.resolve(
        ResolveInput(channel_id="C1", thread_ts="t1", body="hi")
    )
    assert out.project_id == "projectclaw"
    assert out.locked is True
    locked = await conn.fetchval(
        "SELECT project_id FROM thread_project_lock "
        "WHERE channel_id='C1' AND thread_ts='t1'"
    )
    assert locked == "projectclaw"


@pytest.mark.asyncio
async def test_zero_candidates_returns_ambiguous(pg_schema):
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)
    await conn.execute(
        "INSERT INTO project_registry (project_id, allowed_channels) "
        "VALUES ('mindforum', ARRAY['C1']), ('projectclaw', ARRAY['C1'])"
    )
    resolver = ProjectResolver(conn=conn)
    out = await resolver.resolve(
        ResolveInput(channel_id="C1", thread_ts="t1", body="what shipped this week?")
    )
    assert out.project_id is None
    assert out.ambiguous is True
    assert set(out.candidates) == {"mindforum", "projectclaw"}


@pytest.mark.asyncio
async def test_multiple_candidates_returns_ambiguous_does_not_lock(pg_schema):
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)
    await conn.execute(
        "INSERT INTO project_registry (project_id, allowed_channels) "
        "VALUES ('mindforum', ARRAY['C1']), ('projectclaw', ARRAY['C1'])"
    )
    resolver = ProjectResolver(conn=conn)
    out = await resolver.resolve(
        ResolveInput(
            channel_id="C1",
            thread_ts="t1",
            body="compare mindforum vs projectclaw progress",
        )
    )
    assert out.project_id is None
    assert out.ambiguous is True
    locked = await conn.fetchval(
        "SELECT count(*) FROM thread_project_lock WHERE channel_id='C1' AND thread_ts='t1'"
    )
    assert locked == 0
