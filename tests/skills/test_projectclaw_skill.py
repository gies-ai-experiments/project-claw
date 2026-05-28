"""Tests for the projectclaw skill.

The skill is text — there's no Python in it. These tests pin two things:

1. **Discoverability**: the real ``SkillsLoader`` finds ``projectclaw`` in the
   builtin skills directory and returns frontmatter + body.
2. **Policy invariants**: the load-bearing policy clauses are present in the
   body. If somebody refactors the prose and drops the "do not call any tool
   when project is null" rule, this test catches it.

Both invariants are referenced by ``nanobot/channels/slack.py`` (which attaches
the ``metadata.project`` shape the skill assumes). If the shape changes, the
metadata-shape test below will be the first to fail.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from nanobot.agent.skills import BUILTIN_SKILLS_DIR, SkillsLoader
from nanobot.channels.slack import SlackConfig


@pytest.fixture
def loader(tmp_path: Path) -> SkillsLoader:
    return SkillsLoader(workspace=tmp_path, builtin_skills_dir=BUILTIN_SKILLS_DIR)


def test_projectclaw_skill_is_discoverable(loader: SkillsLoader) -> None:
    names = {s["name"] for s in loader.list_skills(filter_unavailable=False)}
    assert "projectclaw" in names


def test_projectclaw_skill_loads_with_expected_frontmatter(loader: SkillsLoader) -> None:
    content = loader.load_skill("projectclaw")
    assert content is not None
    assert content.startswith("---")
    assert re.search(r"^name:\s*projectclaw\s*$", content, re.MULTILINE)
    assert re.search(r"^description:\s*", content, re.MULTILINE)


def test_projectclaw_skill_forbids_out_of_scope_tool_calls(loader: SkillsLoader) -> None:
    body = loader.load_skill("projectclaw") or ""
    # Must explicitly forbid calling tools with repos/folder_ids not in metadata.project.
    assert re.search(r"never.*tool.*(outside|not in)", body, re.IGNORECASE | re.DOTALL), (
        "skill must forbid out-of-scope tool calls"
    )


def test_projectclaw_skill_refuses_when_project_is_null(loader: SkillsLoader) -> None:
    body = loader.load_skill("projectclaw") or ""
    # Must instruct the agent to NOT call tools when metadata.project is null.
    assert re.search(r"null.*do not call any tool|do not call any tool.*null", body, re.IGNORECASE | re.DOTALL), (
        "skill must instruct the agent to refuse (not guess) when project is null"
    )


def test_projectclaw_skill_requires_partial_answer_on_tool_failure(loader: SkillsLoader) -> None:
    body = loader.load_skill("projectclaw") or ""
    # Must require surfacing tool failures inline rather than silently dropping them.
    assert re.search(r"surface.*failure", body, re.IGNORECASE), (
        "skill must require surfacing tool failures"
    )


def test_projectclaw_skill_forbids_fabrication(loader: SkillsLoader) -> None:
    body = loader.load_skill("projectclaw") or ""
    assert re.search(r"never fabricate", body, re.IGNORECASE), (
        "skill must forbid fabricating content"
    )


def test_metadata_project_shape_matches_skill_contract() -> None:
    """The shape SlackChannel emits must match what the skill prose describes.

    The skill says metadata.project is either null OR an object with
    ``name``, optional ``github.repos``, and optional ``granola.tag``. If
    that contract drifts, the skill prose silently lies. This test pins
    the contract by serializing a Project via the real validators.
    """
    cfg = SlackConfig.model_validate(
        {
            "project_map": {
                "C0123ABCDE": {
                    "name": "foo",
                    "github": {"repos": ["acme/foo-api"]},
                    "granola": {"folder_id": "fld_foo"},
                }
            }
        }
    )
    dumped = cfg.project_map["C0123ABCDE"].model_dump()
    assert dumped["name"] == "foo"
    assert dumped["github"]["repos"] == ["acme/foo-api"]
    assert dumped["granola"]["folder_id"] == "fld_foo"

    # Optional sources must serialise as None (not missing) so the skill's
    # ``| null`` reading is true.
    only_github = SlackConfig.model_validate(
        {
            "project_map": {
                "C0123ABCDE": {"name": "foo", "github": {"repos": ["acme/foo"]}}
            }
        }
    ).project_map["C0123ABCDE"].model_dump()
    assert only_github["granola"] is None
