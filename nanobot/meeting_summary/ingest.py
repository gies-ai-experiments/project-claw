"""Ingest a Granola meeting note into L1 memory so the distiller makes L2 facts.

project_id is the project NAME (see project_registry). Idempotent by note id via
MessageStore's ON CONFLICT (channel_type, channel_id, slack_ts).
"""
from __future__ import annotations

from typing import Any, Optional

from nanobot.config.schema import Project
from nanobot.store.message_store import AppendArgs


def build_ingest_args(project: Project, note: dict[str, Any], channel_id: str) -> AppendArgs:
    note_id = str(note.get("id") or "")
    body_parts = [
        note.get("title") or "",
        note.get("summary") or "",
        note.get("transcript") or "",
    ]
    body = "\n\n".join(p for p in body_parts if p).strip()
    return AppendArgs(
        channel_type="granola",
        channel_id=channel_id,
        thread_ts=f"granola:{note_id}",
        project_id=project.name,
        user_id=None,
        role="user",
        body=body or note_id,
        slack_ts=note_id,
    )


async def ingest_note(
    store: Any, project: Project, note: dict[str, Any], channel_id: str
) -> Optional[int]:
    if not note.get("id"):
        return None
    return await store.append(build_ingest_args(project, note, channel_id))
