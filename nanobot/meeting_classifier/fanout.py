"""Pure helpers for the meeting-classifier approval + fan-out flow.

Kept free of I/O so they are unit-testable without Slack, Granola, or the agent.
The classifier skill replies with a JSON array; the admin's button values encode
``mtg-<decision>:<note_id>:<project>``.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from pydantic import ValidationError

from nanobot.meeting_classifier.models import ProjectDraft

_APPROVE = "mtg-approve"
_SKIP = "mtg-skip"


def parse_classification(content: str, known_projects: set[str]) -> list[dict[str, Any]]:
    """Parse the classifier reply into per-project drafts, dropping unknown projects.

    Tolerates a stray markdown fence around the JSON. Returns [] on any parse
    failure (the caller treats that as 'no project matched').
    """
    text = (content or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("["):] if "[" in text else text
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    out: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        project = str(item.get("project") or "").strip()
        if project not in known_projects:
            continue
        out.append({
            "project": project,
            "summary": str(item.get("summary") or "").strip(),
            "actions": [str(a).strip() for a in (item.get("actions") or []) if str(a).strip()],
        })
    return out


def parse_structured_classification(
    content: str, known_projects: set[str]
) -> list[ProjectDraft]:
    """Parse validated project/task drafts, dropping invalid or unknown entries."""
    text = (content or "").strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline == -1:
            return []
        text = text[first_newline + 1:]
        if text.endswith("```"):
            text = text[:-3].rstrip()
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []

    drafts: list[ProjectDraft] = []
    for item in data:
        try:
            draft = ProjectDraft.model_validate(item)
        except (ValidationError, TypeError):
            continue
        is_known_project = draft.project in known_projects
        if (is_known_project and not draft.is_new_project) or (
            not is_known_project and draft.is_new_project
        ):
            drafts.append(draft)
    return drafts


def button_value(decision: str, note_id: str, project: str) -> str:
    return f"mtg-{decision}:{note_id}:{project}"


def parse_action(value: str) -> Optional[tuple[str, str, str]]:
    """Parse a button value into (decision, note_id, project); None if not ours.

    note_id and project never contain ':', so a 3-way split is safe.
    """
    if not value or not (value.startswith(_APPROVE + ":") or value.startswith(_SKIP + ":")):
        return None
    head, _, rest = value.partition(":")
    note_id, _, project = rest.partition(":")
    decision = "approve" if head == _APPROVE else "skip"
    if not note_id or not project:
        return None
    return decision, note_id, project


def build_approval(note_title: str, note_id: str, drafts: list[dict[str, Any]]) -> tuple[str, list]:
    """Build the (text, buttons) for the admin approval DM.

    buttons is a list of rows; each project gets an Approve + Skip row with values
    that encode (note_id, project). Returns ("", []) when there are no drafts.
    """
    if not drafts:
        return "", []
    lines = [f"*Meeting:* {note_title or note_id}", "", "Classified per project — approve or skip each:"]
    buttons: list[list[Any]] = []
    for d in drafts:
        proj = d["project"]
        lines.append("")
        lines.append(f"*{proj}*")
        if d.get("summary"):
            lines.append(d["summary"])
        for a in d.get("actions") or []:
            lines.append(f"• {a}")
        buttons.append([
            [f"✓ Approve {proj}", button_value("approve", note_id, proj)],
            [f"— Skip {proj}", button_value("skip", note_id, proj)],
        ])
    return "\n".join(lines), buttons


def format_post(project: str, note_title: str, draft: dict[str, Any]) -> str:
    """The message posted into a project's channel once its slice is approved."""
    lines = [f"*{note_title or 'Meeting'}* — {project}"]
    if draft.get("summary"):
        lines.append(draft["summary"])
    actions = draft.get("actions") or []
    if actions:
        lines.append("")
        lines.append("Action items:")
        lines.extend(f"• {a}" for a in actions)
    return "\n".join(lines)
