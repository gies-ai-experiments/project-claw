"""Poll one shared Granola folder for new meeting notes.

Deterministic detection only (no LLM here). Each new note is handed to
``on_new_note``, which classifies it per project and routes drafts to the admin.
Dedup by note id (bounded), with a wall-clock watermark so a fresh/restarted
state never backfills the whole meeting history — mirrors MeetingSummaryService.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from loguru import logger

from nanobot.agent.tools.granola import GranolaToolConfig, _granola_get

OnNewNote = Callable[[dict[str, Any]], Awaitable[None]]

_SEEN_LIMIT = 100


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class MeetingClassifierService:
    def __init__(
        self,
        granola_cfg: GranolaToolConfig,
        folder_id: str,
        on_new_note: OnNewNote,
        state_path: Path,
        interval_s: int = 900,
        now_fn: Callable[[], str] = _now_iso,
    ) -> None:
        self.granola_cfg = granola_cfg
        self.folder_id = folder_id
        self.on_new_note = on_new_note
        self.state_path = state_path
        self.interval_s = interval_s
        self._now = now_fn
        self._state: dict[str, Any] = _load_state(state_path)
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if not self.folder_id:
            logger.info("Meeting-classifier: no folder configured; not starting")
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Meeting-classifier started (every {}s, folder {})",
                    self.interval_s, self.folder_id)

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self.interval_s)
                if self._running:
                    await self.tick()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Meeting-classifier loop error")

    async def tick(self) -> None:
        if not self.folder_id:
            return
        # Lazy seed: first tick sets since=now so pre-existing notes aren't backfilled.
        if "since" not in self._state:
            self._state = {"since": self._now(), "seen": []}
        resp = await _granola_get(
            self.granola_cfg, "/notes",
            params={"folder_id": self.folder_id, "created_after": self._state["since"]},
        )
        if isinstance(resp, str):  # error string — keep watermark, retry next tick
            logger.warning("Meeting-classifier: Granola list failed: {}", resp)
            return
        notes = resp.get("notes") or []
        seen = self._state["seen"]
        new_notes = [n for n in notes if n.get("id") and n["id"] not in seen]
        for note in new_notes:
            try:
                await self.on_new_note(note)
            except Exception:
                logger.exception("Meeting-classifier: on_new_note failed for {}", note.get("id"))
            seen.append(note["id"])
        self._state["seen"] = seen[-_SEEN_LIMIT:]
        self._state["since"] = self._now()
        _save_state(self.state_path, self._state)


def _load_state(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(path: Path, state: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        logger.warning("Meeting-classifier: could not persist state to {}", path)
