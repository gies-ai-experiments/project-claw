from uuid import UUID

from nanobot.meeting_classifier.models import MeetingAction, ProjectDraft, TaskDraft
from nanobot.meeting_classifier.slack_ui import (
    build_review_pages,
    build_task_modal,
    parse_task_submission,
)


def test_review_pages_have_header_and_ten_task_boundaries() -> None:
    approval_id = UUID("11111111-1111-1111-1111-111111111111")
    draft = ProjectDraft(
        project="atlas",
        summary="Meeting summary",
        tasks=[TaskDraft(id=f"t{i}", title=f"Task {i}") for i in range(11)],
    )
    pages = build_review_pages(approval_id, 2, "Weekly Sync", draft)
    assert len(pages) == 3
    values = [
        element["value"]
        for page in pages
        for block in page
        for element in block.get("elements", [])
        if "value" in element
    ]
    assert sum("mtg2:approve" in value for value in values) == 1
    assert sum("mtg2:edit" in value for value in values) == 11
    assert sum("mtg2:remove" in value for value in values) == 11
    assert all(":2:" in value for value in values)


def test_modal_round_trips_task_and_marks_manual_date() -> None:
    action = MeetingAction(
        "edit", UUID("11111111-1111-1111-1111-111111111111"), 3, "t1"
    )
    modal = build_task_modal(action, TaskDraft(id="t1", title="Old"))
    assert modal["private_metadata"] == action.encode()
    view = {
        "private_metadata": action.encode(),
        "state": {"values": {
            "title": {"value": {"value": "Ship sync"}},
            "owner_name": {"value": {"value": "Ash"}},
            "owner_email": {"value": {"value": "ash@example.edu"}},
            "collaborators": {"value": {"value": "Sam|sam@example.edu"}},
            "due_on": {"value": {"selected_date": "2026-07-24"}},
        }},
    }
    task = parse_task_submission(view)
    assert task.title == "Ship sync"
    assert task.owner.email == "ash@example.edu"
    assert task.collaborators[0].email == "sam@example.edu"
    assert task.due_on_source == "reviewer"
