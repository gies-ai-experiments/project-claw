"""Tests for the L1 conversation-memory block builder."""
from __future__ import annotations

import pytest

from nanobot.agent.context import conversation_memory_block
from nanobot.store.message_store import AppendArgs, MessageStore
from nanobot.store.migrations import apply_migrations


@pytest.mark.asyncio
async def test_conversation_memory_block_prepends_recent_thread(pg_schema):
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)
    store = MessageStore(conn=conn)
    for i, (role, body) in enumerate(
        [("user", "q1"), ("assistant", "a1"), ("user", "q2")]
    ):
        await store.append(
            AppendArgs(
                channel_type="slack",
                channel_id="C1",
                thread_ts="t1",
                project_id="mindforum",
                user_id="U1" if role == "user" else None,
                role=role,
                body=body,
                slack_ts=f"172{i}.0",
            )
        )
    block = await conversation_memory_block(
        store, channel_id="C1", thread_ts="t1", limit=10
    )
    assert "[Conversation Memory]" in block
    assert block.index("q1") < block.index("a1") < block.index("q2")


@pytest.mark.asyncio
async def test_conversation_memory_block_empty_thread_returns_blank(pg_schema):
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)
    store = MessageStore(conn=conn)
    block = await conversation_memory_block(
        store, channel_id="C1", thread_ts="absent", limit=10
    )
    assert block == ""
