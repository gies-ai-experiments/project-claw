from pathlib import Path


def test_project_digest_skill_is_plain_english_changelog():
    p = Path("nanobot/skills/project-digest/SKILL.md")
    text = p.read_text(encoding="utf-8")
    assert text.startswith("---")
    assert "name: project-digest" in text
    assert "gh " in text                    # reads GitHub (commits / merged PRs)
    assert "100 words" in text              # hard word cap
    assert "non-technical" in text.lower()  # plain-English framing
    assert "project_context_search" not in text  # no longer a memory comparison
