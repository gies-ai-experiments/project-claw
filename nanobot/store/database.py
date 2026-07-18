"""General ProjectClaw database bootstrap, independent of memory injection."""

from __future__ import annotations

import asyncpg
from loguru import logger

from nanobot.channels.slack import ProjectChannel, SlackConfig
from nanobot.config.schema import Config
from nanobot.store.migrations import apply_migrations
from nanobot.store.pool import init_pool
from nanobot.store.runtime_registry import RuntimeProjectRegistry


async def setup_database(
    config: Config, slack_config: SlackConfig | None
) -> asyncpg.Pool | None:
    """Open/migrate the shared database and hydrate live dynamic projects."""
    dsn = config.database.dsn or config.memory.dsn
    if not dsn:
        return None

    pool: asyncpg.Pool | None = None
    failed = False
    try:
        pool = await init_pool(dsn)
        async with pool.acquire() as conn:
            await apply_migrations(conn)
            if slack_config is not None:
                registry = RuntimeProjectRegistry(conn)
                await registry.seed_static(slack_config)
                dynamic = await registry.load_dynamic()
                for project in dynamic:
                    slack_config.projects[project.name] = project
                    if project.channel:
                        slack_config.project_channels[project.channel] = ProjectChannel(
                            allowed_projects=[project.name],
                            default_project=project.name,
                        )
    except Exception:
        failed = True

    if failed:
        if config.integrations.asana.enabled or config.database.dsn:
            raise RuntimeError("ProjectClaw database setup failed.")
        logger.warning("legacy memory database setup failed; continuing without memory")
        return None
    return pool
