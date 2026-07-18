"""Once-per-day-per-project digest scheduler.

Fired by the `daily-digest` system cron job. A per-project date watermark makes
a repeated same-day tick a no-op, so a coarse cron (or a boot-time catch-up)
never double-posts.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from loguru import logger

from nanobot.config.schema import Project

OnDigest = Callable[[Project], Awaitable[None]]


def _channel(p: Project) -> str:
    dd = p.daily_digest
    if dd and dd.digest_channel:
        return dd.digest_channel
    if p.channel:
        return p.channel
    ms = p.meeting_summary
    return ms.summary_channel if ms else ""


def _watched(p: Project) -> bool:
    # "Every channel": any project with a channel and a GitHub repo source is
    # covered when the global gateway.dailyDigest is on — no per-project opt-in.
    has_repos = bool(p.github and (p.github.repos or p.github.org))
    return bool(_channel(p) and has_repos)


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class DailyDigestService:
    def __init__(
        self,
        projects: list[Project],
        on_digest: OnDigest,
        state_path: Path,
        now_date_fn: Callable[[], str] = _today,
    ) -> None:
        self.projects = [p for p in projects if _watched(p)]
        self.on_digest = on_digest
        self.state_path = state_path
        self._now_date = now_date_fn
        self._state: dict[str, str] = _load_state(state_path)

    async def tick(self) -> None:
        today = self._now_date()
        for project in self.projects:
            if self._state.get(project.name) == today:
                continue
            try:
                await self.on_digest(project)
                self._state[project.name] = today
                _save_state(self.state_path, self._state)
            except Exception:
                logger.exception("daily-digest: project '%s' tick failed", project.name)


def _load_state(path: Path) -> dict[str, str]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(path: Path, state: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        logger.warning("daily-digest: could not persist state to %s", path)
