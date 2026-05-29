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
async def test_apply_migrations_creates_project_registry_and_lock(pg_schema):
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)
    pr_cols = {
        r["column_name"]
        for r in await conn.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema=$1 AND table_name='project_registry'",
            schema,
        )
    }
    assert {"project_id", "github_repos", "granola_folder_id", "allowed_channels"} <= pr_cols
    lock_cols = {
        r["column_name"]
        for r in await conn.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema=$1 AND table_name='thread_project_lock'",
            schema,
        )
    }
    assert {"channel_id", "thread_ts", "project_id", "locked_at"} <= lock_cols


@pytest.mark.asyncio
async def test_apply_migrations_creates_project_facts(pg_schema):
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)
    cols = {
        r["column_name"]
        for r in await conn.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema=$1 AND table_name='project_facts'",
            schema,
        )
    }
    assert {
        "id",
        "project_id",
        "kind",
        "subject",
        "body",
        "source_message_ids",
        "confidence",
        "distiller_version",
        "embedding",
        "created_at",
        "superseded_by",
    } <= cols


@pytest.mark.asyncio
async def test_apply_migrations_is_idempotent(pg_schema):
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)
    # Second run must be a no-op (no error, version row not duplicated).
    await apply_migrations(conn, schema=schema)
    versions = await conn.fetch("SELECT version FROM schema_version ORDER BY version")
    assert len(versions) == len({v["version"] for v in versions})
