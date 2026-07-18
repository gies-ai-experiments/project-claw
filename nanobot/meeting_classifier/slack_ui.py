"""Pure Slack Block Kit rendering and task-modal parsing."""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError

from nanobot.meeting_classifier.models import (
    MeetingAction,
    PersonRef,
    ProjectDraft,
    TaskDraft,
)


class TaskSubmissionError(ValueError):
    def __init__(self, errors: dict[str, str]) -> None:
        self.response_action = {"response_action": "errors", "errors": errors}
        super().__init__("invalid task submission")


def build_review_pages(
    approval_id: UUID,
    revision: int,
    meeting_title: str,
    draft: ProjectDraft,
) -> list[list[dict[str, Any]]]:
    lead = (
        f"{draft.lead.name} <{draft.lead.email}>" if draft.lead else "Not applicable"
    )
    header = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*{meeting_title}*\n*Project:* {draft.display_name or draft.project}\n"
                    f"*Lead:* {lead}\n{draft.summary or 'No summary'}"
                ),
            },
        },
        {
            "type": "actions",
            "elements": [
                _button("Add task", MeetingAction("add", approval_id, revision).encode()),
                _button("Skip project", MeetingAction("skip", approval_id, revision).encode()),
                _button(
                    "Approve all",
                    MeetingAction("approve", approval_id, revision).encode(),
                    style="primary",
                ),
            ],
        },
    ]
    pages = [header]
    for offset in range(0, len(draft.tasks), 10):
        blocks: list[dict[str, Any]] = []
        for task in draft.tasks[offset : offset + 10]:
            owner = (
                f"{task.owner.name} <{task.owner.email}>" if task.owner else "Unassigned"
            )
            collaborators = ", ".join(
                f"{person.name} <{person.email}>" for person in task.collaborators
            ) or "None"
            due = task.due_on.isoformat() if task.due_on else "No due date"
            blocks.extend(
                [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": (
                                f"*{task.title}*\nOwner: {owner}\n"
                                f"Collaborators: {collaborators}\nDue: {due}"
                            ),
                        },
                    },
                    {
                        "type": "actions",
                        "elements": [
                            _button(
                                "Edit",
                                MeetingAction(
                                    "edit", approval_id, revision, task.id
                                ).encode(),
                            ),
                            _button(
                                "Remove",
                                MeetingAction(
                                    "remove", approval_id, revision, task.id
                                ).encode(),
                                style="danger",
                            ),
                        ],
                    },
                ]
            )
        pages.append(blocks)
    return pages


def build_task_modal(action: MeetingAction, task: TaskDraft | None) -> dict[str, Any]:
    task = task or TaskDraft(id=action.task_id or "new", title="New task")
    owner = task.owner
    collaborators = "\n".join(
        f"{person.name}|{person.email}" for person in task.collaborators
    )
    return {
        "type": "modal",
        "callback_id": "mtg2-task",
        "private_metadata": action.encode(),
        "title": {"type": "plain_text", "text": "Meeting task"},
        "submit": {"type": "plain_text", "text": "Save"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            _input("title", "Title", task.title),
            _input("owner_name", "Owner name", owner.name if owner else "", optional=True),
            _input("owner_email", "Owner email", owner.email if owner else "", optional=True),
            _input("collaborators", "Collaborators (Name|email per line)", collaborators, optional=True, multiline=True),
            {
                "type": "input",
                "block_id": "due_on",
                "optional": True,
                "label": {"type": "plain_text", "text": "Due date"},
                "element": {
                    "type": "datepicker",
                    "action_id": "value",
                    **({"initial_date": task.due_on.isoformat()} if task.due_on else {}),
                },
            },
        ],
    }


def parse_task_submission(view: dict[str, Any]) -> TaskDraft:
    action = MeetingAction.parse(str(view.get("private_metadata") or ""))
    errors: dict[str, str] = {}
    if action is None or action.verb not in {"add", "edit"}:
        raise TaskSubmissionError({"title": "This task form is stale or invalid."})
    title = _state_value(view, "title").strip()
    if not title:
        errors["title"] = "Title is required."
    owner_name = _state_value(view, "owner_name").strip()
    owner_email = _state_value(view, "owner_email").strip().lower()
    if bool(owner_name) != bool(owner_email):
        errors["owner_email"] = "Provide both owner name and email."
    collaborators: list[PersonRef] = []
    for line in _state_value(view, "collaborators").splitlines():
        if not line.strip():
            continue
        if "|" not in line:
            errors["collaborators"] = "Use Name|email, one person per line."
            continue
        name, email = (part.strip() for part in line.split("|", 1))
        if not name or not email:
            errors["collaborators"] = "Use Name|email, one person per line."
            continue
        collaborators.append(PersonRef(name=name, email=email))
    due_raw = _state_value(view, "due_on", date_value=True).strip()
    due_on: date | None = None
    if due_raw:
        try:
            due_on = date.fromisoformat(due_raw)
        except ValueError:
            errors["due_on"] = "Enter a valid date."
    if errors:
        raise TaskSubmissionError(errors)
    try:
        return TaskDraft(
            id=action.task_id or f"task-{uuid4().hex[:12]}",
            title=title,
            owner=(PersonRef(name=owner_name, email=owner_email) if owner_email else None),
            collaborators=collaborators,
            due_on=due_on,
            due_on_source="reviewer" if due_on else None,
        )
    except ValidationError:
        raise TaskSubmissionError({"title": "Review the task fields."}) from None


def _button(text: str, value: str, *, style: str | None = None) -> dict[str, Any]:
    button: dict[str, Any] = {
        "type": "button",
        "text": {"type": "plain_text", "text": text},
        "value": value,
    }
    if style:
        button["style"] = style
    return button


def _input(
    block_id: str,
    label: str,
    value: str,
    *,
    optional: bool = False,
    multiline: bool = False,
) -> dict[str, Any]:
    element: dict[str, Any] = {"type": "plain_text_input", "action_id": "value"}
    if value:
        element["initial_value"] = value
    if multiline:
        element["multiline"] = True
    return {
        "type": "input",
        "block_id": block_id,
        "optional": optional,
        "label": {"type": "plain_text", "text": label[:2000]},
        "element": element,
    }


def _state_value(view: dict[str, Any], block_id: str, *, date_value: bool = False) -> str:
    block = (((view.get("state") or {}).get("values") or {}).get(block_id) or {})
    action = block.get("value") or next(iter(block.values()), {})
    key = "selected_date" if date_value else "value"
    return str(action.get(key) or "")
