from __future__ import annotations

import os

import asyncpg
import pytest

from nanobot.channels.slack import SlackConfig
from nanobot.config.schema import Project
from nanobot.meeting_classifier.models import PersonRef, ProjectDraft
from nanobot.store.migrations import apply_migrations
from nanobot.store.runtime_registry import RuntimeProjectRegistry


@pytest.mark.asyncio
async def test_static_seed_preserves_dynamic_external_ids(pg_schema):
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)
    registry = RuntimeProjectRegistry(conn)
    dynamic = Project(name="new-lab", asana={"projectGid": "asana-1"})
    await registry.activate_dynamic(dynamic, "CNEW", "asana-1")

    static = SlackConfig.model_validate(
        {"projects": {"new-lab": {"name": "new-lab", "github": {"repos": ["org/lab"]}}}}
    )
    await registry.seed_static(static)

    row = await conn.fetchrow("SELECT * FROM project_registry WHERE project_id='new-lab'")
    assert row["asana_project_gid"] == "asana-1"
    assert row["slack_channel_id"] == "CNEW"
    assert row["github_repos"] == ["org/lab"]
    assert row["source"] == "runtime"


@pytest.mark.asyncio
@pytest.mark.parametrize("static_lead", ["different@example.edu", ""])
async def test_static_seed_preserves_runtime_lead_and_membership(pg_schema, static_lead):
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)
    registry = RuntimeProjectRegistry(conn)
    draft = ProjectDraft(
        project="new-lab",
        is_new_project=True,
        display_name="New Lab",
        description="Runtime description",
        channel_slug="new-lab",
        lead=PersonRef(name="Runtime Lead", email="runtime-lead@example.edu"),
    )
    await registry.reserve_new_project(draft, "UAPPROVER")
    await registry.activate_dynamic(
        Project(name="new-lab", asana={"projectGid": "asana-runtime"}),
        "CRUNTIME",
        "asana-runtime",
    )

    static = SlackConfig.model_validate(
        {
            "projects": {
                "new-lab": {
                    "name": "Static Display",
                    "github": {"repos": ["org/static-lab"]},
                    "leadEmail": static_lead,
                    "description": "Static description",
                }
            }
        }
    )
    await registry.seed_static(static)

    row = await conn.fetchrow("SELECT * FROM project_registry WHERE project_id='new-lab'")
    assert row["lead_email"] == "runtime-lead@example.edu"
    assert row["source"] == "runtime"
    assert row["slack_channel_id"] == "CRUNTIME"
    assert row["asana_project_gid"] == "asana-runtime"
    assert row["github_repos"] == ["org/static-lab"]
    assert row["display_name"] == "Static Display"
    assert row["description"] == "Static description"
    membership = await conn.fetchrow(
        "SELECT * FROM project_membership WHERE project_id='new-lab'"
    )
    assert membership["email_normalized"] == "runtime-lead@example.edu"
    assert membership["role"] == "lead"


@pytest.mark.asyncio
async def test_registry_reserves_activates_and_loads_dynamic_project(pg_schema):
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)
    registry = RuntimeProjectRegistry(conn)
    draft = ProjectDraft(
        project="new-lab",
        is_new_project=True,
        display_name="New Lab",
        description="A new research lab",
        channel_slug="new-lab",
        lead=PersonRef(name="Lead", email=" Lead@Example.edu "),
    )

    await registry.reserve_new_project(draft, "UAPPROVER")
    reserved = await conn.fetchrow("SELECT * FROM project_registry WHERE project_id='new-lab'")
    assert reserved["source"] == "runtime"
    assert reserved["lifecycle_status"] == "provisioning"
    assert reserved["lead_email"] == "lead@example.edu"
    assert reserved["channel_slug"] == "new-lab"

    project = Project(name="new-lab", asana={"projectGid": "asana-22"})
    await registry.activate_dynamic(project, "C22", "asana-22")
    loaded = await registry.load_dynamic()
    assert loaded == [
        Project(
            name="new-lab",
            asana={"projectGid": "asana-22"},
            channel="C22",
            lead_email="lead@example.edu",
            description="A new research lab",
        )
    ]

    await registry.mark_needs_attention("new-lab")
    assert (
        await conn.fetchval(
            "SELECT lifecycle_status FROM project_registry WHERE project_id='new-lab'"
        )
        == "needs_attention"
    )


@pytest.mark.asyncio
async def test_reservation_rejects_channel_slug_collision_and_records_participants(pg_schema):
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)
    registry = RuntimeProjectRegistry(conn)
    first = ProjectDraft(
        project="new-lab",
        is_new_project=True,
        display_name="New Lab",
        description="Research",
        channel_slug="shared-lab",
        lead=PersonRef(name="Lead", email="lead@example.edu"),
        tasks=[{
            "id": "t1",
            "title": "Ship",
            "owner": {"name": "Ash", "email": "ash@example.edu"},
            "collaborators": [{"name": "Jordan", "email": "jordan@example.edu"}],
        }],
    )
    await registry.reserve_new_project(first, "U1")
    collision = first.model_copy(
        update={"project": "other-lab", "display_name": "Other Lab"}
    )
    with pytest.raises(ValueError, match="channel slug"):
        await registry.reserve_new_project(collision, "U1")

    memberships = await conn.fetch(
        "SELECT email_normalized, role FROM project_membership ORDER BY email_normalized"
    )
    assert [dict(row) for row in memberships] == [
        {"email_normalized": "ash@example.edu", "role": "participant"},
        {"email_normalized": "jordan@example.edu", "role": "participant"},
        {"email_normalized": "lead@example.edu", "role": "lead"},
    ]


@pytest.mark.asyncio
async def test_reservation_creates_one_lead_and_preserves_verified_identity(pg_schema):
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)
    await conn.execute(
        """
        INSERT INTO identity_directory
          (email_normalized, display_name, slack_user_id, asana_user_gid, verified_at)
        VALUES ('lead@example.edu', 'Old Name', 'U_LEAD', 'A_LEAD', now())
        """
    )
    registry = RuntimeProjectRegistry(conn)
    draft = ProjectDraft(
        project="new-lab",
        is_new_project=True,
        display_name="New Lab",
        description="A new research lab",
        channel_slug="new-lab",
        lead=PersonRef(name="New Name", email=" Lead@Example.edu "),
    )
    await registry.reserve_new_project(draft, "UAPPROVER")

    identity = await conn.fetchrow(
        "SELECT * FROM identity_directory WHERE email_normalized='lead@example.edu'"
    )
    assert identity["display_name"] == "New Name"
    assert identity["slack_user_id"] == "U_LEAD"
    assert identity["asana_user_gid"] == "A_LEAD"
    assert identity["verified_at"] is not None
    memberships = await conn.fetch("SELECT * FROM project_membership")
    assert [(row["email_normalized"], row["role"]) for row in memberships] == [
        ("lead@example.edu", "lead")
    ]

    conflicting = draft.model_copy(
        update={"lead": PersonRef(name="Other", email="other@example.edu")}
    )
    with pytest.raises(ValueError, match="different lead"):
        await registry.reserve_new_project(conflicting, "UAPPROVER")
    registry_row = await conn.fetchrow(
        "SELECT lead_email FROM project_registry WHERE project_id='new-lab'"
    )
    assert registry_row["lead_email"] == "lead@example.edu"
    assert await conn.fetchval(
        "SELECT COUNT(*) FROM identity_directory WHERE email_normalized='other@example.edu'"
    ) == 0
    assert await conn.fetchval("SELECT COUNT(*) FROM identity_directory") == 1
    membership = await conn.fetchrow(
        "SELECT email_normalized, role FROM project_membership WHERE project_id='new-lab'"
    )
    assert dict(membership) == {"email_normalized": "lead@example.edu", "role": "lead"}


@pytest.mark.asyncio
async def test_activation_rejects_static_owner_and_normalizes_runtime_lead(pg_schema):
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)
    registry = RuntimeProjectRegistry(conn)
    static = SlackConfig.model_validate(
        {"projects": {"atlas": {"name": "Atlas", "github": {"repos": ["org/atlas"]}}}}
    )
    await registry.seed_static(static)
    with pytest.raises(ValueError, match="static configuration"):
        await registry.activate_dynamic(
            Project(name="atlas", asana={"projectGid": "A1"}), "C1", "A1"
        )
    row = await conn.fetchrow("SELECT * FROM project_registry WHERE project_id='atlas'")
    assert row["source"] == "static_config"
    assert row["slack_channel_id"] is None
    assert row["asana_project_gid"] is None

    await conn.execute(
        """
        INSERT INTO project_registry (project_id, source, lifecycle_status)
        VALUES ('runtime', 'runtime', 'provisioning')
        """
    )
    await registry.activate_dynamic(
        Project(
            name="runtime",
            asana={"projectGid": "A2"},
            lead_email=" Lead@Example.EDU ",
        ),
        "C2",
        "A2",
    )
    assert (
        await conn.fetchval("SELECT lead_email FROM project_registry WHERE project_id='runtime'")
        == "lead@example.edu"
    )


@pytest.mark.asyncio
async def test_runtime_registry_transactions_accept_pool(pg_schema):
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)

    async def setup(pool_conn):
        await pool_conn.execute(f'SET search_path TO "{schema}", public')

    pool = await asyncpg.create_pool(
        os.environ["PROJECTCLAW_TEST_PG_DSN"], min_size=1, max_size=2, setup=setup
    )
    try:
        registry = RuntimeProjectRegistry(pool)
        await registry.seed_static(
            SlackConfig.model_validate(
                {"projects": {"atlas": {"name": "atlas", "github": {"repos": ["o/a"]}}}}
            )
        )
        draft = ProjectDraft(
            project="new-lab",
            is_new_project=True,
            display_name="New Lab",
            description="A new research lab",
            channel_slug="new-lab",
            lead=PersonRef(name="Lead", email="lead@example.edu"),
        )
        await registry.reserve_new_project(draft, "U1")
        assert await pool.fetchval("SELECT COUNT(*) FROM project_membership") == 1
    finally:
        await pool.close()
