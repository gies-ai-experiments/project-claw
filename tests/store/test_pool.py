"""Smoke test for the asyncpg pool lifecycle."""
from __future__ import annotations

import os

import pytest

from nanobot.store.pool import close_pool, init_pool

PG_DSN = os.environ.get(
    "PROJECTCLAW_TEST_PG_DSN",
    "postgresql://projectclaw:changeme@localhost:5432/projectclaw",
)


@pytest.mark.asyncio
async def test_pool_open_close():
    import asyncpg

    try:
        pool = await init_pool(PG_DSN)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"no Postgres reachable at {PG_DSN}: {exc}")
    try:
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT 1") == 1
    finally:
        await close_pool()
