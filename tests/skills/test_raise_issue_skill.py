"""raise-issue skill is discoverable, always-on, and gated on the gh binary."""
from pathlib import Path

import pytest

from nanobot.agent.skills import SkillsLoader

NONEXISTENT_WS = Path("/tmp/nanobot-raise-issue-test-ws-does-not-exist")


@pytest.fixture
def loader() -> SkillsLoader:
    return SkillsLoader(NONEXISTENT_WS)


def test_skill_is_discovered(loader: SkillsLoader) -> None:
    names = {s["name"] for s in loader.list_skills(filter_unavailable=False)}
    assert "raise-issue" in names


def test_skill_metadata_parses(loader: SkillsLoader) -> None:
    meta = loader.get_skill_metadata("raise-issue")
    assert meta is not None
    assert meta.get("description")
    nb = loader._parse_nanobot_metadata(meta.get("metadata"))
    assert nb.get("always") is True
    assert nb.get("requires", {}).get("bins") == ["gh"]


def test_skill_body_loads_without_frontmatter(loader: SkillsLoader) -> None:
    body = loader.load_skills_for_context(["raise-issue"])
    assert "Raise GitHub Issue" in body
    assert "metadata:" not in body  # frontmatter stripped


def test_always_and_requires_gate(loader: SkillsLoader, monkeypatch) -> None:
    # gh present -> in get_always_skills; absent -> filtered out
    import nanobot.agent.skills as skills_mod

    monkeypatch.setattr(skills_mod.shutil, "which", lambda _cmd: "/usr/bin/gh")
    assert "raise-issue" in loader.get_always_skills()

    monkeypatch.setattr(skills_mod.shutil, "which", lambda _cmd: None)
    assert "raise-issue" not in loader.get_always_skills()
