from __future__ import annotations

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
    assert await conn.fetchval(
        "SELECT lifecycle_status FROM project_registry WHERE project_id='new-lab'"
    ) == "needs_attention"
