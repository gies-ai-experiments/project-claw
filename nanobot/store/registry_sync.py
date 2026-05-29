"""Hydrate the project_registry table from Slack config on boot.

Idempotent upsert: the config (channel-local `projects` + `project_channels`)
is the source of truth, so this can run on every boot.
"""
from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

import asyncpg

if TYPE_CHECKING:
    from nanobot.channels.slack import SlackConfig


async def sync_project_registry(conn: asyncpg.Connection, slack_cfg: "SlackConfig") -> None:
    channels_for: dict[str, list[str]] = defaultdict(list)
    for chan_id, pc in slack_cfg.project_channels.items():
        for name in pc.allowed_projects:
            channels_for[name].append(chan_id)

    async with conn.transaction():
        for name, project in slack_cfg.projects.items():
            repos = project.github.repos if project.github else []
            folder = project.granola.folder_id if project.granola else None
            await conn.execute(
                """
                INSERT INTO project_registry
                  (project_id, github_repos, granola_folder_id, allowed_channels)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (project_id) DO UPDATE
                  SET github_repos = EXCLUDED.github_repos,
                      granola_folder_id = EXCLUDED.granola_folder_id,
                      allowed_channels = EXCLUDED.allowed_channels
                """,
                name,
                repos,
                folder,
                sorted(set(channels_for.get(name, []))),
            )
