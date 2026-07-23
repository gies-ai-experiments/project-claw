"""Poll each project repo's default branch for new commits and hand them to a
callback that posts a plain-English update to the project channel.

Near-real-time without a GitHub webhook: it only *reads* commits (the same
access the daily digest uses), so no org/repo webhook-admin approval is needed.
A per-repo commit-SHA watermark (persisted) means a restart never re-posts, and
the first tick seeds the watermark without backfilling history — mirrors
MeetingClassifierService / DailyDigestService.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
from loguru import logger

from nanobot.config.schema import Project
from nanobot.daily_digest.service import _channel

# on_new(project_name, channel, repo, new_commit_subjects) -> awaitable
OnNewCommits = Callable[[str, str, str, list[str]], Awaitable[None]]

_GITHUB_API = "https://api.github.com"
_PER_PAGE = 30  # newest commits fetched per repo per tick (also caps a burst)


def build_repo_channel_map(projects: dict[str, Project]) -> dict[str, tuple[str, str]]:
    """``repo -> (project_name, channel)`` for every project that has a channel."""
    out: dict[str, tuple[str, str]] = {}
    for name, project in projects.items():
        channel = _channel(project)
        if not channel:
            continue
        github = getattr(project, "github", None)
        for repo in github.repos if github and github.repos else []:
            out[repo.strip()] = (name, channel)
    return out


class GithubPollService:
    def __init__(
        self,
        repo_channels: dict[str, tuple[str, str]],
        token: str,
        on_new: OnNewCommits,
        state_path: Path,
        interval_s: int = 300,
    ) -> None:
        self.repo_channels = repo_channels
        self.token = token
        self.on_new = on_new
        self.state_path = state_path
        self.interval_s = interval_s
        self._state: dict[str, str] = _load_state(state_path)  # repo -> last-seen sha
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if not self.repo_channels:
            logger.info("github-poll: no repos configured; not starting")
            return
        if not self.token:
            logger.warning("github-poll: no GH token in env; not starting")
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            "github-poll started (every {}s, {} repos)", self.interval_s, len(self.repo_channels)
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
                logger.exception("github-poll loop error")

    async def tick(self) -> None:
        changed = False
        async with httpx.AsyncClient(timeout=20) as client:
            for repo, (project, channel) in self.repo_channels.items():
                try:
                    commits = await self._list_commits(client, repo)
                except Exception:
                    logger.exception("github-poll: list failed for {}", repo)
                    continue
                if not commits:
                    continue
                newest = commits[0].get("sha", "")
                seen = self._state.get(repo)
                if seen is None:  # lazy seed: record HEAD, do not backfill
                    self._state[repo] = newest
                    changed = True
                    continue
                if not newest or newest == seen:
                    continue
                subjects: list[str] = []
                for commit in commits:  # newest-first
                    if commit.get("sha") == seen:
                        break
                    msg = (commit.get("commit", {}).get("message") or "").split("\n", 1)[0].strip()
                    if msg:
                        subjects.append(msg)
                if subjects:
                    try:
                        await self.on_new(project, channel, repo, subjects)
                    except Exception:
                        logger.exception("github-poll: on_new failed for {}", repo)
                self._state[repo] = newest
                changed = True
        if changed:
            _save_state(self.state_path, self._state)

    async def _list_commits(self, client: httpx.AsyncClient, repo: str) -> list[dict[str, Any]]:
        """Newest-first commits on the repo's default branch (empty on any error)."""
        resp = await client.get(
            f"{_GITHUB_API}/repos/{repo}/commits",
            params={"per_page": _PER_PAGE},
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        if resp.status_code != 200:
            logger.warning("github-poll: {} -> HTTP {}", repo, resp.status_code)
            return []
        data = resp.json()
        return data if isinstance(data, list) else []


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
        logger.warning("github-poll: could not persist state to {}", path)
