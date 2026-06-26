"""Tests for syncing the project_registry from Slack config."""
from __future__ import annotations

import pytest

from nanobot.channels.slack import SlackConfig
from nanobot.store.migrations import apply_migrations
from nanobot.store.registry_sync import sync_project_registry


@pytest.mark.asyncio
async def test_sync_writes_projects_and_allowed_channels(pg_schema):
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)
    cfg = SlackConfig.model_validate(
        {
            "projects": {
                "mindforum": {
                    "name": "mindforum",
                    "github": {"repos": ["org/MindForum"]},
                    "granola": {"folderId": "fol_x"},
                },
                "projectclaw": {
                    "name": "projectclaw",
                    "github": {"repos": ["org/project-claw"]},
                },
            },
            "projectChannels": {
                "C1": {"allowedProjects": ["mindforum", "projectclaw"]},
                "C2": {"allowedProjects": ["projectclaw"]},
            },
        }
    )
    await sync_project_registry(conn, cfg)
    rows = await conn.fetch("SELECT * FROM project_registry ORDER BY project_id")
    by_id = {r["project_id"]: r for r in rows}
    assert sorted(by_id["mindforum"]["allowed_channels"]) == ["C1"]
    assert sorted(by_id["projectclaw"]["allowed_channels"]) == ["C1", "C2"]
    assert by_id["mindforum"]["granola_folder_id"] == "fol_x"
    assert sorted(by_id["mindforum"]["github_repos"]) == ["org/MindForum"]


@pytest.mark.asyncio
async def test_sync_writes_default_channels(pg_schema):
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)
    cfg = SlackConfig.model_validate(
        {
            "projects": {
                "mindforum": {"name": "mindforum", "github": {"repos": ["org/MindForum"]}},
                "gies-lab": {"name": "gies-lab", "granola": {"folderId": "fol_X"}},
            },
            "projectChannels": {
                "C1": {
                    "allowedProjects": ["mindforum", "gies-lab"],
                    "defaultProject": "gies-lab",
                },
            },
        }
    )
    await sync_project_registry(conn, cfg)
    rows = await conn.fetch("SELECT * FROM project_registry ORDER BY project_id")
    by_id = {r["project_id"]: r for r in rows}
    assert sorted(by_id["gies-lab"]["default_channels"]) == ["C1"]
    assert sorted(by_id["mindforum"]["default_channels"]) == []


@pytest.mark.asyncio
async def test_sync_is_idempotent_and_updates(pg_schema):
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)
    cfg = SlackConfig.model_validate(
        {
            "projects": {"foo": {"name": "foo", "github": {"repos": ["acme/foo"]}}},
            "projectChannels": {"C1": {"allowedProjects": ["foo"]}},
        }
    )
    await sync_project_registry(conn, cfg)
    await sync_project_registry(conn, cfg)  # second run: no dup, no error
    count = await conn.fetchval("SELECT COUNT(*) FROM project_registry")
    assert count == 1

    cfg2 = SlackConfig.model_validate(
        {
            "projects": {"foo": {"name": "foo", "github": {"repos": ["acme/foo2"]}}},
            "projectChannels": {"C1": {"allowedProjects": ["foo"]},
                                "C9": {"allowedProjects": ["foo"]}},
        }
    )
    await sync_project_registry(conn, cfg2)
    row = await conn.fetchrow("SELECT * FROM project_registry WHERE project_id='foo'")
    assert sorted(row["github_repos"]) == ["acme/foo2"]
    assert sorted(row["allowed_channels"]) == ["C1", "C9"]
