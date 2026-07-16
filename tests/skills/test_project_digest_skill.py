from pathlib import Path


def test_project_digest_skill_exists_and_has_frontmatter():
    p = Path("nanobot/skills/project-digest/SKILL.md")
    text = p.read_text(encoding="utf-8")
    assert text.startswith("---")
    assert "name: project-digest" in text
    assert "project_context_search" in text  # reads L2 memory
    assert "gh " in text                       # reads live GitHub
