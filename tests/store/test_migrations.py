"""Tests for the migration runner and schema."""

from __future__ import annotations

import asyncpg
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


@pytest.mark.asyncio
async def test_apply_migrations_creates_asana_sync_storage(pg_schema):
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)

    registry_columns = {
        row["column_name"]
        for row in await conn.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema=$1 AND table_name='project_registry'",
            schema,
        )
    }
    assert {
        "display_name",
        "description",
        "lead_email",
        "slack_channel_id",
        "asana_project_gid",
        "lifecycle_status",
        "source",
        "created_by_slack_id",
        "channel_slug",
        "created_at",
        "updated_at",
    } <= registry_columns

    tables = {
        row["table_name"]
        for row in await conn.fetch(
            "SELECT table_name FROM information_schema.tables WHERE table_schema=$1",
            schema,
        )
    }
    assert {
        "identity_directory",
        "project_membership",
        "meeting_approval",
        "provisioning_job",
        "provisioning_step",
    } <= tables


@pytest.mark.asyncio
async def test_asana_sync_constraints_enforce_registry_and_lead_invariants(pg_schema):
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)

    index = await conn.fetchrow(
        "SELECT indexdef FROM pg_indexes WHERE schemaname=$1 "
        "AND indexname='project_membership_one_lead'",
        schema,
    )
    assert index is not None
    assert "WHERE (role = 'lead'::text)" in index["indexdef"]
    assert await conn.fetchval("SELECT MAX(version) FROM schema_version") == 7

    await conn.execute("INSERT INTO project_registry (project_id) VALUES ('atlas')")
    row = await conn.fetchrow(
        "SELECT lifecycle_status, source FROM project_registry WHERE project_id='atlas'"
    )
    assert dict(row) == {"lifecycle_status": "active", "source": "static_config"}
    await conn.executemany(
        "INSERT INTO identity_directory (email_normalized, display_name) VALUES ($1, $2)",
        [("one@example.edu", "One"), ("two@example.edu", "Two")],
    )
    await conn.execute("INSERT INTO project_membership VALUES ('atlas', 'one@example.edu', 'lead')")
    with pytest.raises(asyncpg.UniqueViolationError, match="project_membership_one_lead"):
        await conn.execute(
            "INSERT INTO project_membership VALUES ('atlas', 'two@example.edu', 'lead')"
        )
    with pytest.raises(asyncpg.CheckViolationError, match="project_membership_role_check"):
        await conn.execute(
            "INSERT INTO project_membership VALUES ('atlas', 'two@example.edu', 'owner')"
        )
    with pytest.raises(
        asyncpg.ForeignKeyViolationError, match="project_membership_project_id_fkey"
    ):
        await conn.execute(
            "INSERT INTO project_membership VALUES ('missing', 'two@example.edu', 'participant')"
        )
