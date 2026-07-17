from pathlib import Path


def test_meeting_classify_skill_exists_and_specifies_json_output():
    p = Path("nanobot/skills/meeting-classify/SKILL.md")
    text = p.read_text(encoding="utf-8")
    assert text.startswith("---")
    assert "name: meeting-classify" in text
    assert "JSON" in text                 # structured, parseable output
    assert "projects" in text             # matches against the project registry
    assert "[]" in text                   # empty result contract
