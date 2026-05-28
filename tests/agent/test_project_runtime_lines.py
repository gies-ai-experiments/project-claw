"""Tests for project_runtime_lines — surfaces inbound metadata.project to the LLM.

Without this, the projectclaw skill talks about a metadata.project field that
the LLM cannot see in the prompt (it's a Python-side InboundMessage attribute).
These tests pin that the function emits exactly enough text for the skill's
scoping rules to have referents.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nanobot.agent.context import project_runtime_lines


def _msg(metadata):
    return SimpleNamespace(metadata=metadata)


def test_no_metadata_returns_empty():
    assert project_runtime_lines(_msg(None)) == []


def test_metadata_without_project_returns_empty():
    assert project_runtime_lines(_msg({"slack": {"thread_ts": "1.0"}})) == []


def test_project_explicitly_null_returns_empty():
    assert project_runtime_lines(_msg({"project": None})) == []


def test_project_with_github_and_granola_emits_all_three_lines():
    lines = project_runtime_lines(
        _msg(
            {
                "project": {
                    "name": "foo",
                    "github": {"repos": ["acme/foo-api", "acme/foo-web"]},
                    "granola": {"folder_id": "fol_abc"},
                }
            }
        )
    )
    joined = "\n".join(lines)
    assert "project.name: foo" in joined
    assert "acme/foo-api" in joined and "acme/foo-web" in joined
    assert "fol_abc" in joined


def test_project_github_only_omits_granola_line():
    lines = project_runtime_lines(
        _msg({"project": {"name": "foo", "github": {"repos": ["acme/foo"]}}})
    )
    joined = "\n".join(lines)
    assert "project.name: foo" in joined
    assert "github" in joined
    assert "granola" not in joined


def test_project_granola_only_omits_github_line():
    lines = project_runtime_lines(
        _msg({"project": {"name": "foo", "granola": {"folder_id": "fol_xyz"}}})
    )
    joined = "\n".join(lines)
    assert "project.name: foo" in joined
    assert "granola" in joined
    assert "github" not in joined


def test_project_with_no_name_returns_empty():
    """A project dict without a name is malformed — defensive: skip the block."""
    assert (
        project_runtime_lines(_msg({"project": {"github": {"repos": ["a/b"]}}})) == []
    )


def test_project_with_empty_repos_omits_github_line():
    lines = project_runtime_lines(
        _msg(
            {
                "project": {
                    "name": "foo",
                    "github": {"repos": []},
                    "granola": {"folder_id": "fol_x"},
                }
            }
        )
    )
    joined = "\n".join(lines)
    assert "project.name: foo" in joined
    assert "github" not in joined
    assert "fol_x" in joined


@pytest.mark.parametrize("bad_metadata", ["not-a-dict", 42, [], object()])
def test_non_mapping_metadata_returns_empty(bad_metadata):
    assert project_runtime_lines(_msg(bad_metadata)) == []
