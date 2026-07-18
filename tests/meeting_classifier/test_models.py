from datetime import date
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from nanobot.meeting_classifier.models import (
    ApprovalSnapshot,
    MeetingAction,
    ProjectDraft,
    TaskDraft,
)


def test_task_requires_due_date_source():
    task = TaskDraft.model_validate({
        "id": "task-1",
        "title": "Ship sync",
        "owner": {"name": "Ashleyn", "email": " ASHLEYN@EXAMPLE.EDU "},
        "dueOn": "2026-07-24",
        "dueOnSource": "meeting",
    })

    assert task.due_on.isoformat() == "2026-07-24"
    assert task.owner is not None
    assert task.owner.email == "ashleyn@example.edu"


@pytest.mark.parametrize(
    "due_on,due_on_source",
    [("2026-07-24", None), (None, "reviewer")],
)
def test_task_rejects_mismatched_due_date_and_source(due_on, due_on_source):
    with pytest.raises(ValidationError):
        TaskDraft.model_validate({
            "id": "task-1",
            "title": "Ship sync",
            "dueOn": due_on,
            "dueOnSource": due_on_source,
        })


def test_task_rejects_blank_title_and_duplicate_collaborator_emails():
    with pytest.raises(ValidationError):
        TaskDraft(id="task-1", title="  ")

    with pytest.raises(ValidationError):
        TaskDraft.model_validate({
            "id": "task-1",
            "title": "Ship sync",
            "collaborators": [
                {"name": "One", "email": "person@example.edu"},
                {"name": "Two", "email": " PERSON@example.edu "},
            ],
        })


def test_new_project_requires_creation_fields_and_exactly_one_lead():
    valid = {
        "project": "new-lab",
        "isNewProject": True,
        "displayName": "New Lab",
        "description": "Research project",
        "channelSlug": "new-lab",
        "lead": {"name": "Lead", "email": "lead@example.edu"},
    }
    assert ProjectDraft.model_validate(valid).lead.email == "lead@example.edu"

    for missing in ("displayName", "description", "channelSlug", "lead"):
        invalid = dict(valid)
        invalid.pop(missing)
        with pytest.raises(ValidationError):
            ProjectDraft.model_validate(invalid)


def test_project_rejects_duplicate_task_ids():
    with pytest.raises(ValidationError):
        ProjectDraft.model_validate({
            "project": "atlas",
            "tasks": [
                {"id": "t1", "title": "First"},
                {"id": "t1", "title": "Second"},
            ],
        })


def test_approval_snapshot_accepts_camel_and_snake_case_fields():
    approval_id = uuid4()
    snapshot = ApprovalSnapshot.model_validate({
        "approvalId": str(approval_id),
        "note_id": "note-1",
        "meetingTitle": "Weekly",
        "meeting_date": "2026-07-18",
        "revision": 2,
        "draft": {"project": "atlas"},
    })

    assert snapshot.approval_id == approval_id
    assert snapshot.meeting_date == date(2026, 7, 18)


def test_meeting_action_roundtrip():
    approval_id = uuid4()
    action = MeetingAction("edit", approval_id, 3, "task-1")

    encoded = action.encode()

    assert encoded == f"mtg2:edit:{approval_id}:3:task-1"
    assert MeetingAction.parse(encoded) == action
    assert MeetingAction.parse(f"mtg2:approve:{approval_id}:0:-") == MeetingAction(
        "approve", approval_id, 0
    )


@pytest.mark.parametrize(
    "value",
    [
        "",
        "mtg:approve:00000000-0000-0000-0000-000000000000:0:-",
        "mtg2::00000000-0000-0000-0000-000000000000:0:-",
        "mtg2:approve:not-a-uuid:0:-",
        "mtg2:approve:00000000-0000-0000-0000-000000000000:-1:-",
        "mtg2:approve:00000000-0000-0000-0000-000000000000:+1:-",
        "mtg2:approve:00000000-0000-0000-0000-000000000000: 1:-",
        "mtg2:approve:00000000-0000-0000-0000-000000000000:0",
        "mtg2:approve:00000000-0000-0000-0000-000000000000:0:",
    ],
)
def test_meeting_action_rejects_malformed_values(value):
    assert MeetingAction.parse(value) is None


def test_meeting_action_constructor_rejects_invalid_fields():
    approval_id = UUID("00000000-0000-0000-0000-000000000000")
    with pytest.raises(ValueError):
        MeetingAction("", approval_id, 0)
    with pytest.raises(ValueError):
        MeetingAction("approve", approval_id, -1)
    with pytest.raises(ValueError):
        MeetingAction("approve", approval_id, 0, "has:colon")
