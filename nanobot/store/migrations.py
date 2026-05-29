"""Idempotent SQL migration runner for the projectclaw memory store."""
from __future__ import annotations

from pathlib import Path

import asyncpg

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


async def apply_migrations(conn: asyncpg.Connection, schema: str | None = None) -> None:
    """Apply any unapplied numbered migrations in order.

    Migrations live in ``migrations/NNNN_*.sql`` and run inside a transaction
    each; ``schema_version`` records applied versions so re-running is a no-op.
    """
    if schema:
        await conn.execute(f'SET search_path TO "{schema}"')
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version INT PRIMARY KEY,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    applied = {r["version"] for r in await conn.fetch("SELECT version FROM schema_version")}
    for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = int(sql_file.name.split("_", 1)[0])
        if version in applied:
            continue
        async with conn.transaction():
            await conn.execute(sql_file.read_text())
            await conn.execute(
                "INSERT INTO schema_version (version) VALUES ($1)", version
            )
