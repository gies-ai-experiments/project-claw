"""Integration test for the AgentLoop memory wiring (resolve/persist/inject)."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from nanobot.bus.events import InboundMessage
from nanobot.channels.project_resolver import ProjectResolver
from nanobot.store.message_store import MessageStore
from nanobot.store.migrations import apply_migrations


def _provider() -> MagicMock:
    prov = MagicMock()  # no spec: tolerates the full AgentLoop construction surface
    prov.get_default_model.return_value = "test-model"
    prov.generation = SimpleNamespace(max_tokens=4096, temperature=0.1, reasoning_effort=None)
    return prov


def _msg(content: str) -> InboundMessage:
    return InboundMessage(
        channel="slack",
        sender_id="U1",
        chat_id="C1",
        content=content,
        metadata={"slack": {"thread_ts": "t1", "event": {"ts": "111.0"}}},
        session_key_override="slack:C1:t1",
    )


@pytest.mark.asyncio
async def test_loop_resolves_persists_and_injects_memory(pg_schema, loop_factory):
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)
    await conn.execute(
        "INSERT INTO project_registry (project_id, allowed_channels) "
        "VALUES ('mindforum', ARRAY['C1'])"
    )
    loop = loop_factory(provider=_provider())
    loop.attach_memory(MessageStore(conn), ProjectResolver(conn), inject_limit=20)

    # the L2 search tool registers when memory is attached
    assert loop.tools.has("project_context_search")

    msg = _msg("hello mindforum")
    ctx = SimpleNamespace(msg=msg, all_messages=[], save_skip=0)

    # 1. resolve sets project_id (single allowed project for C1)
    await loop._memory_resolve_project(ctx)
    assert msg.metadata["project_id"] == "mindforum"

    # 2. first turn: no prior thread history
    assert await loop._memory_fetch_block(ctx) is None

    # 3. persist inbound, then outbound (assistant + tool rows from this turn)
    await loop._memory_persist_inbound(ctx)
    ctx.all_messages = [
        {"role": "user", "content": "hello mindforum"},
        {"role": "assistant", "content": "hi, working on it"},
    ]
    ctx.save_skip = 1  # skip the user row; persist the assistant row
    await loop._memory_persist_outbound(ctx)

    rows = await conn.fetch("SELECT role, body, project_id FROM messages ORDER BY id")
    by_role = {r["role"] for r in rows}
    assert {"user", "assistant"} <= by_role
    assert all(r["project_id"] == "mindforum" for r in rows)

    # 4. next turn: the L1 block now carries the prior thread, oldest-first
    block = await loop._memory_fetch_block(ctx)
    assert block is not None
    assert "[Conversation Memory]" in block
    assert block.index("hello mindforum") < block.index("hi, working on it")


@pytest.mark.asyncio
async def test_loop_injects_github_repos_into_project_metadata(pg_schema, loop_factory):
    """The resolved project must carry github.repos into metadata so the renderer
    surfaces the real owner/name slug to the model (not just the project name)."""
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)
    await conn.execute(
        "INSERT INTO project_registry (project_id, github_repos, allowed_channels) "
        "VALUES ('mindforum', ARRAY['gies-ai-experiments/MindForum'], ARRAY['C1'])"
    )
    loop = loop_factory(provider=_provider())
    loop.attach_memory(MessageStore(conn), ProjectResolver(conn), inject_limit=20)

    msg = _msg("hello mindforum")
    ctx = SimpleNamespace(msg=msg, all_messages=[], save_skip=0)
    await loop._memory_resolve_project(ctx)

    project = msg.metadata["project"]
    assert project["name"] == "mindforum"
    assert project["github"]["repos"] == ["gies-ai-experiments/MindForum"]


@pytest.mark.asyncio
async def test_loop_injects_granola_folder_id_into_project_metadata(pg_schema, loop_factory):
    """The resolved project must carry granola.folder_id into metadata so the
    renderer surfaces it and Granola tool calls scope to the right folder."""
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)
    await conn.execute(
        "INSERT INTO project_registry (project_id, granola_folder_id, allowed_channels) "
        "VALUES ('mindforum', 'fol_PJTxBtvhzqzImI', ARRAY['C1'])"
    )
    loop = loop_factory(provider=_provider())
    loop.attach_memory(MessageStore(conn), ProjectResolver(conn), inject_limit=20)

    msg = _msg("any meeting notes?")
    ctx = SimpleNamespace(msg=msg, all_messages=[], save_skip=0)
    await loop._memory_resolve_project(ctx)

    project = msg.metadata["project"]
    assert project["name"] == "mindforum"
    assert project["granola"]["folder_id"] == "fol_PJTxBtvhzqzImI"


@pytest.mark.asyncio
async def test_loop_without_memory_is_inert(pg_schema, loop_factory):
    loop = loop_factory(provider=_provider())  # no attach_memory
    assert not loop.tools.has("project_context_search")
    ctx = SimpleNamespace(msg=_msg("hi"), all_messages=[], save_skip=0)
    # all helpers are safe no-ops when no store/resolver is wired
    await loop._memory_resolve_project(ctx)
    assert "project_id" not in ctx.msg.metadata
    assert await loop._memory_fetch_block(ctx) is None
    await loop._memory_persist_inbound(ctx)
    await loop._memory_persist_outbound(ctx)
