"""Seed config-owned project registry fields from Slack config on boot."""
from __future__ import annotations

from typing import TYPE_CHECKING

import asyncpg

from nanobot.store.runtime_registry import RuntimeProjectRegistry

if TYPE_CHECKING:
    from nanobot.channels.slack import SlackConfig


async def sync_project_registry(conn: asyncpg.Connection, slack_cfg: "SlackConfig") -> None:
    await RuntimeProjectRegistry(conn).seed_static(slack_cfg)
