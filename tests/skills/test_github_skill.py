"""Tests for the github skill.

The skill is text — these tests pin the load-bearing clauses that make
the projectclaw scoping work. Mirrors test_projectclaw_skill.py.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from nanobot.agent.skills import BUILTIN_SKILLS_DIR, SkillsLoader


@pytest.fixture
def loader(tmp_path: Path) -> SkillsLoader:
    return SkillsLoader(workspace=tmp_path, builtin_skills_dir=BUILTIN_SKILLS_DIR)


def _nb_meta(loader: SkillsLoader) -> dict:
    raw = (loader.get_skill_metadata("github") or {}).get("metadata")
    if isinstance(raw, str):
        raw = json.loads(raw)
    return (raw or {}).get("nanobot") or {}


def test_github_skill_is_discoverable(loader: SkillsLoader) -> None:
    names = {s["name"] for s in loader.list_skills(filter_unavailable=False)}
    assert "github" in names


def test_github_skill_is_always_on(loader: SkillsLoader) -> None:
    """If somebody flips always:false in frontmatter, this catches it."""
    assert _nb_meta(loader).get("always") is True


def test_github_skill_requires_scoping_to_metadata_project(loader: SkillsLoader) -> None:
    body = loader.load_skill("github") or ""
    assert re.search(r"metadata\.project\.github\.repos", body)
    assert re.search(r"never query a repo not in", body, re.IGNORECASE)


def test_github_skill_provides_open_prs_query(loader: SkillsLoader) -> None:
    body = loader.load_skill("github") or ""
    assert re.search(r"gh pr list .*--state open", body, re.DOTALL)
    assert re.search(r"isDraft\s*=\s*false", body, re.IGNORECASE)


def test_github_skill_provides_recently_merged_query(loader: SkillsLoader) -> None:
    body = loader.load_skill("github") or ""
    assert re.search(r"gh pr list .*--state merged.*--search.*merged:", body, re.DOTALL)


def test_github_skill_provides_issues_query(loader: SkillsLoader) -> None:
    body = loader.load_skill("github") or ""
    assert re.search(r"gh issue list .*--search.*created:.*closed:", body, re.DOTALL)


def test_github_skill_issue_search_uses_repo_qualifier(loader: SkillsLoader) -> None:
    """Regression: without `repo:<repo>` INSIDE --search, an OR clause makes gh
    fall back to a GLOBAL issues search and return issues from random repos.
    The `--repo` flag alone is insufficient. See the MindForum incident
    on 2026-05-28: the bot was handed issues from Andrei-Ciuperca/Practica_Anul_4,
    ChanyaVRC/pyrust, etc. — completely unrelated repos.
    """
    body = loader.load_skill("github") or ""
    # The issues query section must include `repo:<repo>` in the search string.
    match = re.search(
        r"gh issue list .*--search\s+\"repo:<repo>.*created:.*closed:",
        body,
        re.DOTALL,
    )
    assert match is not None, (
        "issues query is missing `repo:<repo>` inside --search; "
        "this leaks results from unrelated repos when the search has an OR."
    )


def test_github_skill_pr_merged_search_uses_repo_qualifier(loader: SkillsLoader) -> None:
    """Defensive: PR merged query also uses `repo:<repo>` inside --search,
    so it stays correct even if the search clause later grows an OR.
    """
    body = loader.load_skill("github") or ""
    match = re.search(
        r"gh pr list .*--state merged.*--search\s+\"repo:<repo>.*merged:",
        body,
        re.DOTALL,
    )
    assert match is not None, (
        "PR merged query is missing `repo:<repo>` inside --search; "
        "future edits adding an OR clause could silently leak."
    )


def test_github_skill_requires_partial_answer_on_failure(loader: SkillsLoader) -> None:
    body = loader.load_skill("github") or ""
    assert re.search(r"surface the failure", body, re.IGNORECASE)


def test_github_skill_forbids_fabrication(loader: SkillsLoader) -> None:
    body = loader.load_skill("github") or ""
    assert re.search(r"never invent|never fabricate", body, re.IGNORECASE)


def test_github_skill_refuses_when_project_is_null(loader: SkillsLoader) -> None:
    body = loader.load_skill("github") or ""
    assert re.search(
        r"refuse.*null|null.*refuse|metadata\.project.*null.*ask",
        body,
        re.IGNORECASE | re.DOTALL,
    )


def test_github_skill_cites_with_owner_repo_number_format(loader: SkillsLoader) -> None:
    body = loader.load_skill("github") or ""
    assert "owner/repo#NUMBER" in body or "`owner/repo#" in body
