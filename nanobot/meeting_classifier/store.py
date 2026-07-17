"""Pending per-(note, project) approval drafts for the meeting classifier.

Keyed by ``<note_id>\x00<project>``. A draft starts ``pending``; the admin's
button click transitions it once to ``approved``/``skipped``. ``mark`` returns
True only on that first transition, so a re-click or a restart never re-posts.
Atomic write mirrors the meeting-summary state pattern.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from loguru import logger


def _key(note_id: str, project: str) -> str:
    return f"{note_id}\x00{project}"


class ApprovalStore:
    def __init__(self, state_path: Path) -> None:
        self.state_path = state_path
        self._state: dict[str, dict[str, Any]] = _load(state_path)

    def add_draft(self, note_id: str, project: str, draft: dict[str, Any]) -> None:
        self._state.setdefault(_key(note_id, project), {"draft": draft, "status": "pending"})
        self._save()

    def get_draft(self, note_id: str, project: str) -> Optional[dict[str, Any]]:
        return self._state.get(_key(note_id, project))

    def status(self, note_id: str, project: str) -> Optional[str]:
        entry = self._state.get(_key(note_id, project))
        return entry["status"] if entry else None

    def mark(self, note_id: str, project: str, status: str) -> bool:
        """Transition pending → status. Returns True only on the first decision."""
        entry = self._state.get(_key(note_id, project))
        if not entry or entry["status"] != "pending":
            return False
        entry["status"] = status
        self._save()
        return True

    def _save(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
            tmp.write_text(json.dumps(self._state), encoding="utf-8")
            os.replace(tmp, self.state_path)
        except OSError:
            logger.warning("meeting-classifier: could not persist store to {}", self.state_path)


def _load(path: Path) -> dict[str, dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
