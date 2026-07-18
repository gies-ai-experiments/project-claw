from pathlib import Path


def test_meeting_classify_skill_exists_and_specifies_json_output():
    p = Path("nanobot/skills/meeting-classify/SKILL.md")
    text = p.read_text(encoding="utf-8")
    assert text.startswith("---")
    assert "name: meeting-classify" in text
    assert "JSON" in text                 # structured, parseable output
    assert "projects" in text             # matches against the project registry
    assert "[]" in text                   # empty result contract


def test_meeting_classify_skill_specifies_structured_project_task_contract():
    text = Path("nanobot/skills/meeting-classify/SKILL.md").read_text(encoding="utf-8")
    lower_text = text.lower()

    for field in (
        '"isNewProject"',
        '"displayName"',
        '"description"',
        '"channelSlug"',
        '"lead"',
        '"tasks"',
        '"owner"',
        '"collaborators"',
        '"dueOn"',
        '"dueOnSource"',
    ):
        assert field in text
    assert "name+email" in text
    assert "exact known project names" in text
    assert "distinct project discussed as newly formed" in text
    assert "only emit a due date stated in the note" in lower_text
    assert "one primary owner" in text
