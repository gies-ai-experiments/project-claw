"""Tests for the migration runner and schema."""
from __future__ import annotations

import pytest

from nanobot.store.migrations import apply_migrations


@pytest.mark.asyncio
async def test_apply_migrations_creates_messages_table(pg_schema):
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)
    cols = await conn.fetch(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema=$1 AND table_name='messages'",
        schema,
    )
    names = {c["column_name"] for c in cols}
    assert {
        "id",
        "channel_type",
        "channel_id",
        "thread_ts",
        "project_id",
        "user_id",
        "role",
        "body",
        "tool_calls",
        "slack_ts",
        "created_at",
        "distilled_at",
    } <= names


@pytest.mark.asyncio
async def test_apply_migrations_is_idempotent(pg_schema):
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)
    # Second run must be a no-op (no error, version row not duplicated).
    await apply_migrations(conn, schema=schema)
    versions = await conn.fetch("SELECT version FROM schema_version ORDER BY version")
    assert len(versions) == len({v["version"] for v in versions})
