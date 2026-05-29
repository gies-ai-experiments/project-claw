"""Tests for MessageStore append/idempotency/truncation."""
from __future__ import annotations

import pytest

from nanobot.store.message_store import AppendArgs, MessageStore
from nanobot.store.migrations import apply_migrations


@pytest.mark.asyncio
async def test_append_inserts_row(pg_schema):
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)
    store = MessageStore(conn=conn)
    new_id = await store.append(
        AppendArgs(
            channel_type="slack",
            channel_id="C1",
            thread_ts="t1",
            project_id="mindforum",
            user_id="U1",
            role="user",
            body="hello",
            slack_ts="1716937812.000100",
        )
    )
    assert new_id is not None
    row = await conn.fetchrow("SELECT * FROM messages WHERE id=$1", new_id)
    assert row["body"] == "hello"


@pytest.mark.asyncio
async def test_append_is_idempotent_on_slack_ts(pg_schema):
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)
    store = MessageStore(conn=conn)
    args = AppendArgs(
        channel_type="slack",
        channel_id="C1",
        thread_ts="t1",
        project_id="mindforum",
        user_id="U1",
        role="user",
        body="hello",
        slack_ts="1716937812.000100",
    )
    first = await store.append(args)
    second = await store.append(args)
    assert first is not None and second is None  # second is the no-op
    count = await conn.fetchval("SELECT COUNT(*) FROM messages")
    assert count == 1


@pytest.mark.asyncio
async def test_tool_row_body_truncated_to_500_chars(pg_schema):
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)
    store = MessageStore(conn=conn)
    long_summary = "x" * 2000
    await store.append(
        AppendArgs(
            channel_type="slack",
            channel_id="C1",
            thread_ts="t1",
            project_id="mindforum",
            user_id=None,
            role="tool",
            body=long_summary,
            slack_ts="1716937812.000200",
            tool_calls={"name": "exec", "args": {"cmd": "gh pr list"}},
        )
    )
    body = await conn.fetchval(
        "SELECT body FROM messages WHERE slack_ts='1716937812.000200'"
    )
    assert len(body) == 500
    tool_calls = await conn.fetchval(
        "SELECT tool_calls FROM messages WHERE slack_ts='1716937812.000200'"
    )
    # JSONB round-trips; asyncpg returns it as a JSON string by default.
    assert "exec" in tool_calls
