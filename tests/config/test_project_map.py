"""Tests for projectclaw per-channel project mapping models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nanobot.config.schema import (
    GitHubProjectConfig,
    GranolaProjectConfig,
    Project,
)


def test_project_with_github_only_is_valid():
    p = Project.model_validate(
        {"name": "foo", "github": {"repos": ["acme/foo-api"]}}
    )
    assert p.name == "foo"
    assert p.github is not None
    assert p.github.repos == ["acme/foo-api"]
    assert p.granola is None


def test_project_with_granola_only_is_valid():
    p = Project.model_validate({"name": "foo", "granola": {"folder_id": "fld_foo"}})
    assert p.granola is not None
    assert p.granola.folder_id == "fld_foo"
    assert p.github is None


def test_project_with_neither_source_is_rejected():
    with pytest.raises(ValidationError) as exc:
        Project.model_validate({"name": "foo"})
    assert "github" in str(exc.value).lower() or "granola" in str(exc.value).lower()


def test_project_with_both_sources_is_valid():
    p = Project.model_validate(
        {
            "name": "foo",
            "github": {"repos": ["acme/foo-api", "acme/foo-web"]},
            "granola": {"folder_id": "fld_foo"},
        }
    )
    assert p.github.repos == ["acme/foo-api", "acme/foo-web"]
    assert p.granola.folder_id == "fld_foo"


def test_github_project_requires_at_least_one_repo():
    with pytest.raises(ValidationError):
        GitHubProjectConfig.model_validate({"repos": []})


def test_granola_project_requires_nonempty_folder_id():
    with pytest.raises(ValidationError):
        GranolaProjectConfig.model_validate({"tag": ""})


# --- SlackConfig.project_map / default_project ---

from nanobot.channels.slack import SlackConfig  # noqa: E402


def _project(name: str = "foo") -> dict:
    return {"name": name, "github": {"repos": [f"acme/{name}"]}}


def test_slack_config_accepts_project_map_keyed_by_channel_id():
    cfg = SlackConfig.model_validate(
        {
            "project_map": {
                "C0123ABCDE": _project("foo"),
                "C0456FGHIJ": _project("bar"),
            }
        }
    )
    assert "C0123ABCDE" in cfg.project_map
    assert cfg.project_map["C0123ABCDE"].name == "foo"


def test_slack_config_rejects_channel_name_as_key():
    with pytest.raises(ValidationError) as exc:
        SlackConfig.model_validate(
            {"project_map": {"#project-foo": _project("foo")}}
        )
    assert "channel id" in str(exc.value).lower()


def test_slack_config_default_project_must_exist_in_map():
    with pytest.raises(ValidationError) as exc:
        SlackConfig.model_validate(
            {
                "project_map": {"C0123ABCDE": _project("foo")},
                "default_project": "bar",
            }
        )
    assert "default_project" in str(exc.value).lower()


def test_slack_config_default_project_resolves_when_valid():
    cfg = SlackConfig.model_validate(
        {
            "project_map": {"C0123ABCDE": _project("foo")},
            "default_project": "foo",
        }
    )
    assert cfg.default_project == "foo"


def test_slack_config_without_project_map_works():
    cfg = SlackConfig.model_validate({})
    assert cfg.project_map == {}
    assert cfg.default_project is None
