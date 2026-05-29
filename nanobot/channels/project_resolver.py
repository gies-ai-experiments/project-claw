"""Resolve which project a Slack thread belongs to, with per-thread sticky locking.

A channel may host several projects. The first turn in a thread that names exactly
one allowed project locks the thread to it; later turns reuse the lock. Naming zero
or several projects is ambiguous and does not lock (the caller prompts the user).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import asyncpg


@dataclass
class ResolveInput:
    channel_id: str
    thread_ts: str
    body: str


@dataclass
class ResolveResult:
    project_id: str | None
    locked: bool = False
    ambiguous: bool = False
    candidates: list[str] = field(default_factory=list)


_PREFIX_RE = re.compile(r"\[([a-zA-Z0-9_\-]+)\]")
_REPO_RE = re.compile(r"([a-zA-Z0-9_\-]+/[a-zA-Z0-9_\-\.]+)")


class ProjectResolver:
    def __init__(self, conn: asyncpg.Connection | asyncpg.Pool):
        self._conn = conn

    async def resolve(self, inp: ResolveInput) -> ResolveResult:
        # 1. lock cache hit
        locked = await self._conn.fetchrow(
            "SELECT project_id FROM thread_project_lock "
            "WHERE channel_id=$1 AND thread_ts=$2",
            inp.channel_id,
            inp.thread_ts,
        )
        if locked:
            return ResolveResult(project_id=locked["project_id"])

        # 2. allowed projects for this channel
        rows = await self._conn.fetch(
            "SELECT project_id, github_repos FROM project_registry "
            "WHERE $1 = ANY(allowed_channels)",
            inp.channel_id,
        )
        allowed = {r["project_id"]: r["github_repos"] for r in rows}
        if not allowed:
            return ResolveResult(project_id=None, ambiguous=True)

        # 3. An explicit [project] prefix is authoritative. It disambiguates even
        #    when the loose signals below are noisy — e.g. the bot's own @mention
        #    ("@projectclaw") collides with a project literally named projectclaw.
        prefix_candidates = {
            m.group(1) for m in _PREFIX_RE.finditer(inp.body) if m.group(1) in allowed
        }
        if len(prefix_candidates) == 1:
            return await self._lock(inp, next(iter(prefix_candidates)))
        if len(prefix_candidates) > 1:
            return ResolveResult(
                project_id=None, ambiguous=True, candidates=sorted(prefix_candidates)
            )

        # 4. Fall back to loose signals: a project name mentioned in the body, or
        #    a known repo slug.
        candidates: set[str] = set()
        body_lower = inp.body.lower()
        for name in allowed:
            if name.lower() in body_lower:
                candidates.add(name)
        for m in _REPO_RE.finditer(inp.body):
            repo = m.group(1)
            for name, repos in allowed.items():
                if repo in repos:
                    candidates.add(name)

        if len(candidates) == 1:
            return await self._lock(inp, next(iter(candidates)))

        return ResolveResult(
            project_id=None,
            ambiguous=True,
            candidates=sorted(allowed.keys()),
        )

    async def _lock(self, inp: ResolveInput, project_id: str) -> ResolveResult:
        await self._conn.execute(
            "INSERT INTO thread_project_lock (channel_id, thread_ts, project_id) "
            "VALUES ($1,$2,$3) ON CONFLICT DO NOTHING",
            inp.channel_id,
            inp.thread_ts,
            project_id,
        )
        return ResolveResult(project_id=project_id, locked=True)
