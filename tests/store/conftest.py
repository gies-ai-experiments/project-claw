"""Shared fixtures for store tests.

Each test gets a throwaway Postgres schema so tests are fully isolated and
self-cleaning. Point `PROJECTCLAW_TEST_PG_DSN` at a reachable pgvector instance
(defaults to the docker-compose service on :5432). Tests are skipped — not
failed — when no Postgres is reachable, so the rest of the suite stays green.
"""
from __future__ import annotations

import os
import uuid

import asyncpg
import pytest
import pytest_asyncio

PG_DSN = os.environ.get(
    "PROJECTCLAW_TEST_PG_DSN",
    "postgresql://projectclaw:changeme@localhost:5432/projectclaw",
)


@pytest_asyncio.fixture
async def pg_schema():
    try:
        conn = await asyncpg.connect(PG_DSN)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"no Postgres reachable at {PG_DSN}: {exc}")
    schema = f"test_{uuid.uuid4().hex[:8]}"
    try:
        await conn.execute(f'CREATE SCHEMA "{schema}"')
        await conn.execute(f'SET search_path TO "{schema}"')
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        yield schema, conn
    finally:
        await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await conn.close()
