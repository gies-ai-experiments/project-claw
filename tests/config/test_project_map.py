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
    p = Project.model_validate({"name": "foo", "granola": {"tag": "foo"}})
    assert p.granola is not None
    assert p.granola.tag == "foo"
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
            "granola": {"tag": "foo"},
        }
    )
    assert p.github.repos == ["acme/foo-api", "acme/foo-web"]
    assert p.granola.tag == "foo"


def test_github_project_requires_at_least_one_repo():
    with pytest.raises(ValidationError):
        GitHubProjectConfig.model_validate({"repos": []})


def test_granola_project_requires_nonempty_tag():
    with pytest.raises(ValidationError):
        GranolaProjectConfig.model_validate({"tag": ""})
