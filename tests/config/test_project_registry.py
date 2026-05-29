"""Tests for the channel-local projects registry + project_channels (Task 6)."""

from __future__ import annotations

from nanobot.channels.slack import SlackConfig


def test_loads_projects_registry_and_project_channels():
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
                "C0123ABCDE": {
                    "allowedProjects": ["mindforum", "projectclaw"],
                    "defaultProject": None,
                },
            },
        }
    )
    assert cfg.projects["mindforum"].github.repos == ["org/MindForum"]
    assert cfg.projects["mindforum"].granola.folder_id == "fol_x"
    assert cfg.project_channels["C0123ABCDE"].allowed_projects == [
        "mindforum",
        "projectclaw",
    ]
    assert cfg.project_channels["C0123ABCDE"].default_project is None


def test_project_channels_accepts_snake_case_keys():
    cfg = SlackConfig.model_validate(
        {
            "projects": {"foo": {"name": "foo", "github": {"repos": ["acme/foo"]}}},
            "project_channels": {
                "C0123ABCDE": {
                    "allowed_projects": ["foo"],
                    "default_project": "foo",
                }
            },
        }
    )
    assert cfg.project_channels["C0123ABCDE"].allowed_projects == ["foo"]
    assert cfg.project_channels["C0123ABCDE"].default_project == "foo"


def test_legacy_project_map_shims_into_registry():
    cfg = SlackConfig.model_validate(
        {
            "project_map": {
                "C0123ABCDE": {"name": "foo", "github": {"repos": ["acme/foo"]}}
            },
            "default_project": "foo",
        }
    )
    # legacy still readable
    assert cfg.project_map["C0123ABCDE"].name == "foo"
    # and projected into the new registry
    assert "foo" in cfg.projects
    assert cfg.project_channels["C0123ABCDE"].allowed_projects == ["foo"]
    assert cfg.project_channels["C0123ABCDE"].default_project == "foo"


def test_empty_config_has_empty_registry():
    cfg = SlackConfig.model_validate({})
    assert cfg.projects == {}
    assert cfg.project_channels == {}
