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
async def test_resolve_returns_github_repos(pg_schema):
    """A resolved project carries its github_repos so the loop can surface the
    real owner/name slug to the model (otherwise gh gets the bare project name)."""
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)
    await conn.execute(
        "INSERT INTO project_registry (project_id, github_repos, allowed_channels) "
        "VALUES ('mindforum', ARRAY['gies-ai-experiments/MindForum'], ARRAY['C1']), "
        "('illinihunt', ARRAY['gies-ai-experiments/illinihunt'], ARRAY['C1'])"
    )
    resolver = ProjectResolver(conn=conn)
    out = await resolver.resolve(
        ResolveInput(channel_id="C1", thread_ts="t1", body="[mindforum] what is open?")
    )
    assert out.project_id == "mindforum"
    assert out.github_repos == ["gies-ai-experiments/MindForum"]


@pytest.mark.asyncio
async def test_locked_thread_returns_github_repos(pg_schema):
    """The lock-cache-hit path must also return the project's github_repos."""
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)
    await conn.execute(
        "INSERT INTO project_registry (project_id, github_repos, allowed_channels) "
        "VALUES ('mindforum', ARRAY['gies-ai-experiments/MindForum'], ARRAY['C1'])"
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
    assert out.github_repos == ["gies-ai-experiments/MindForum"]


@pytest.mark.asyncio
async def test_resolve_returns_granola_folder_id(pg_schema):
    """A resolved project carries its granola_folder_id so the loop can scope
    Granola tool calls to the right folder."""
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)
    await conn.execute(
        "INSERT INTO project_registry (project_id, granola_folder_id, allowed_channels) "
        "VALUES ('mindforum', 'fol_PJTxBtvhzqzImI', ARRAY['C1'])"
    )
    resolver = ProjectResolver(conn=conn)
    out = await resolver.resolve(
        ResolveInput(channel_id="C1", thread_ts="t1", body="any meeting notes?")
    )
    assert out.project_id == "mindforum"
    assert out.granola_folder_id == "fol_PJTxBtvhzqzImI"


@pytest.mark.asyncio
async def test_locked_thread_returns_granola_folder_id(pg_schema):
    """The lock-cache-hit path must also return the project's granola_folder_id."""
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)
    await conn.execute(
        "INSERT INTO project_registry (project_id, granola_folder_id, allowed_channels) "
        "VALUES ('mindforum', 'fol_PJTxBtvhzqzImI', ARRAY['C1'])"
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
    assert out.granola_folder_id == "fol_PJTxBtvhzqzImI"


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
async def test_channel_default_resolves_unnamed_mention_without_locking(pg_schema):
    """A multi-project channel with a configured default resolves an unnamed
    mention to that default (granola-only context), and does NOT lock the
    thread — so a later explicit [project] can still take over."""
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)
    await conn.execute(
        "INSERT INTO project_registry (project_id, github_repos, granola_folder_id, "
        "allowed_channels, default_channels) VALUES "
        "('mindforum', ARRAY['org/MindForum'], NULL, ARRAY['C1'], ARRAY[]::text[]), "
        "('illinihunt', ARRAY['org/illinihunt'], NULL, ARRAY['C1'], ARRAY[]::text[]), "
        "('gies-lab', ARRAY[]::text[], 'fol_X', ARRAY['C1'], ARRAY['C1'])"
    )
    resolver = ProjectResolver(conn=conn)
    out = await resolver.resolve(
        ResolveInput(channel_id="C1", thread_ts="t1", body="what did we cover recently?")
    )
    assert out.project_id == "gies-lab"
    assert out.granola_folder_id == "fol_X"
    assert out.github_repos == []
    assert out.locked is False
    # not locked: the thread can still be claimed by an explicit project later
    n = await conn.fetchval(
        "SELECT count(*) FROM thread_project_lock WHERE channel_id='C1' AND thread_ts='t1'"
    )
    assert n == 0


@pytest.mark.asyncio
async def test_explicit_prefix_beats_channel_default(pg_schema):
    """An explicit [project] still wins over the channel default and locks."""
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)
    await conn.execute(
        "INSERT INTO project_registry (project_id, granola_folder_id, "
        "allowed_channels, default_channels) VALUES "
        "('mindforum', NULL, ARRAY['C1'], ARRAY[]::text[]), "
        "('gies-lab', 'fol_X', ARRAY['C1'], ARRAY['C1'])"
    )
    resolver = ProjectResolver(conn=conn)
    out = await resolver.resolve(
        ResolveInput(channel_id="C1", thread_ts="t1", body="[mindforum] open issues?")
    )
    assert out.project_id == "mindforum"
    assert out.locked is True


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
