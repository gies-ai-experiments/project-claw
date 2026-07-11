"""Poll each opted-in project's Granola folder for new meeting notes.

Deterministic detection only — no LLM here. Each new note is handed to
``on_new_meeting``, which runs the meeting-summary skill and posts the result.
Dedup is by note ID (bounded per folder); the fetch window is a wall-clock
watermark advanced every tick, so a fresh/restarted state never backfills the
whole meeting history.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from loguru import logger

from nanobot.agent.tools.granola import GranolaToolConfig, _granola_get
from nanobot.config.schema import Project

OnNewMeeting = Callable[[Project, dict[str, Any]], Awaitable[None]]

_SEEN_LIMIT = 100  # ponytail: bound per-folder dedup; meetings-per-interval << 100


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _watched(p: Project) -> bool:
    ms = p.meeting_summary
    return bool(p.granola and ms and ms.enabled and ms.summary_channel)


class MeetingSummaryService:
    def __init__(
        self,
        granola_cfg: GranolaToolConfig,
        projects: list[Project],
        on_new_meeting: OnNewMeeting,
        state_path: Path,
        interval_s: int = 900,
        now_fn: Callable[[], str] = _now_iso,
    ) -> None:
        self.granola_cfg = granola_cfg
        self.projects = [p for p in projects if _watched(p)]
        self.on_new_meeting = on_new_meeting
        self.state_path = state_path
        self.interval_s = interval_s
        self._now = now_fn
        self._state: dict[str, dict[str, Any]] = _load_state(state_path)
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if not self.projects:
            logger.info("Meeting-summary: no opted-in projects; not starting")
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            "Meeting-summary started (every {}s, {} projects)",
            self.interval_s, len(self.projects),
        )

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
                logger.exception("Meeting-summary loop error")

    async def tick(self) -> None:
        for project in self.projects:
            try:
                await self._poll_project(project)
            except Exception:
                logger.exception("Meeting-summary: project '{}' tick failed", project.name)

    async def _poll_project(self, project: Project) -> None:
        folder_id = project.granola.folder_id
        # Lazy seed: a folder's first tick sets `since=now`, so pre-existing notes
        # are never backfilled. ponytail: costs a one-interval gap on a folder's
        # very first run; fine for a 15-min poll.
        st = self._state.setdefault(folder_id, {"since": self._now(), "seen": []})
        resp = await _granola_get(
            self.granola_cfg, "/notes",
            params={"folder_id": folder_id, "created_after": st["since"]},
        )
        if isinstance(resp, str):  # error string — leave watermark, retry next tick
            logger.warning("Meeting-summary: Granola list failed for '{}': {}", project.name, resp)
            return
        notes = resp.get("notes") or []
        # ponytail: first page only; add cursor paging if a folder exceeds one page/interval
        seen = st["seen"]
        new_notes = [n for n in notes if n.get("id") and n["id"] not in seen]
        for note in new_notes:
            await self.on_new_meeting(project, note)
            seen.append(note["id"])
        st["seen"] = seen[-_SEEN_LIMIT:]
        st["since"] = self._now()  # advance window; boundary notes still caught by `seen`
        _save_state(self.state_path, self._state)


def _load_state(path: Path) -> dict[str, dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(path: Path, state: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        logger.warning("Meeting-summary: could not persist state to {}", path)
