"""Tests for the L2 distiller — L1 messages → project_facts (Task 16).

Covers: undistilled selection, transcript rendering, JSON parsing (tolerant of
fences and trailing prose), embedding wiring (with and without embedder),
insert + supersession on (project, kind, normalized subject), and the
``distilled_at`` progress mark. Uses the pg_schema throwaway-schema fixture;
tests skip when no Postgres is reachable.
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.providers.base import LLMResponse
from nanobot.store.distiller import _MAX_FACTS_PER_THREAD, Distiller, _normalize_subject
from nanobot.store.migrations import apply_migrations

DIM = 1536


def _vec(seed: int) -> list[float]:
    v = [0.0] * DIM
    v[seed % DIM] = 1.0
    return v


class FakeEmbedder:
    def __init__(self):
        self.calls = 0

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [_vec(abs(hash(t)) % DIM) for t in texts]


def _llm(content: str) -> Any:
    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content=content, finish_reason="stop"))
    return provider


async def _seed_messages(conn, rows: list[tuple[str, str, str, str, str, str]]) -> list[int]:
    """Insert L1 rows (project_id, channel_id, thread_ts, role, body, slack_ts)."""
    ids: list[int] = []
    for project_id, channel_id, thread_ts, role, body, slack_ts in rows:
        rid = await conn.fetchval(
            """
            INSERT INTO messages
              (channel_type, channel_id, thread_ts, project_id, user_id, role, body, slack_ts)
            VALUES ('slack',$1,$2,$3,'U1',$4,$5,$6)
            RETURNING id
            """,
            channel_id, thread_ts, project_id, role, body, slack_ts,
        )
        ids.append(rid)
    return ids


@pytest.mark.asyncio
async def test_distills_facts_from_thread_and_marks_distilled(pg_schema):
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)
    await _seed_messages(conn, [
        ("mf", "C1", "t1", "user", "let's use Plan B for the auth refactor", "1"),
        ("mf", "C1", "t1", "assistant", "Got it — Plan B it is.", "2"),
    ])
    llm = _llm(json.dumps([
        {"kind": "decision", "subject": "Auth approach", "body": "Chose Plan B for the auth refactor."},
    ]))

    d = Distiller(conn, llm, "test-model", embedder=None)
    stats = await d.run_once()

    assert stats["threads"] == 1
    assert stats["messages_distilled"] == 2
    assert stats["facts_inserted"] == 1
    assert stats["errors"] == 0

    rows = await conn.fetch("SELECT distilled_at FROM messages WHERE project_id='mf'")
    assert all(r["distilled_at"] is not None for r in rows)

    fact = await conn.fetchrow("SELECT kind, subject, body, embedding FROM project_facts")
    assert fact["kind"] == "decision"
    assert fact["subject"] == "Auth approach"
    assert fact["embedding"] is None


@pytest.mark.asyncio
async def test_inserts_embedding_when_embedder_present(pg_schema):
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)
    await _seed_messages(conn, [("mf", "C1", "t1", "user", "we chose Postgres", "1")])
    embedder = FakeEmbedder()
    llm = _llm(json.dumps([{"kind": "decision", "subject": "DB", "body": "chose Postgres"}]))
    d = Distiller(conn, llm, "test-model", embedder=embedder)

    await d.run_once()

    assert embedder.calls == 1
    fact = await conn.fetchrow("SELECT embedding FROM project_facts WHERE subject='DB'")
    assert fact["embedding"] is not None


@pytest.mark.asyncio
async def test_supersedes_older_fact_on_same_subject(pg_schema):
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)
    # Pre-existing old fact on the same subject.
    await conn.execute(
        "INSERT INTO project_facts (project_id, kind, subject, body, distiller_version, "
        "source_message_ids) VALUES ('mf','decision','auth approach','Old choice.','v0',ARRAY[99])"
    )
    await _seed_messages(conn, [
        ("mf", "C1", "t1", "user", "actually we changed to Plan C for auth", "1"),
    ])
    llm = _llm(json.dumps([
        {"kind": "decision", "subject": "auth approach", "body": "Chose Plan C"},
    ]))
    d = Distiller(conn, llm, "test-model", embedder=None)

    stats = await d.run_once()
    assert stats["facts_superseded"] == 1

    old = await conn.fetchrow(
        "SELECT superseded_by FROM project_facts WHERE subject='auth approach' AND body='Old choice.'"
    )
    new = await conn.fetchrow(
        "SELECT id, superseded_by FROM project_facts WHERE body='Chose Plan C'"
    )
    assert old["superseded_by"] == new["id"]
    assert new["superseded_by"] is None


@pytest.mark.asyncio
async def test_does_not_supersede_actions_or_open_questions(pg_schema):
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)
    await conn.execute(
        "INSERT INTO project_facts (project_id, kind, subject, body, distiller_version, "
        "source_message_ids) VALUES ('mf','action','send pr','opened #1','v0',ARRAY[99])"
    )
    await _seed_messages(conn, [("mf", "C1", "t1", "user", "open a new PR", "1")])
    llm = _llm(json.dumps([
        {"kind": "action", "subject": "send pr", "body": "opened #2"},
    ]))
    d = Distiller(conn, llm, "test-model", embedder=None)

    stats = await d.run_once()
    assert stats["facts_superseded"] == 0
    rows = await conn.fetch("SELECT superseded_by FROM project_facts")
    assert all(r["superseded_by"] is None for r in rows)


@pytest.mark.asyncio
async def test_parse_tolerates_json_fences_and_prose(pg_schema):
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)
    await _seed_messages(conn, [("mf", "C1", "t1", "user", "decide", "1")])
    content = "Here are the facts:\n```json\n" + json.dumps([
        {"kind": "fact", "subject": "X", "body": "Y"},
    ]) + "\n```\nLet me know."
    llm = _llm(content)
    d = Distiller(conn, llm, "test-model", embedder=None)

    stats = await d.run_once()
    assert stats["facts_inserted"] == 1


@pytest.mark.asyncio
async def test_empty_json_array_logs_no_facts(pg_schema):
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)
    ids = await _seed_messages(conn, [("mf", "C1", "t1", "user", "hi", "1")])
    llm = _llm("[]")
    d = Distiller(conn, llm, "test-model", embedder=None)

    stats = await d.run_once()
    assert stats["facts_inserted"] == 0
    # No facts, but rows still get marked distilled so we don't re-read chitchat.
    rows = await conn.fetch("SELECT distilled_at FROM messages WHERE id=ANY($1)", ids)
    assert all(r["distilled_at"] is not None for r in rows)


@pytest.mark.asyncio
async def test_invalid_kind_is_skipped(pg_schema):
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)
    await _seed_messages(conn, [("mf", "C1", "t1", "user", "x", "1")])
    llm = _llm(json.dumps([
        {"kind": "garbage", "subject": "X", "body": "Y"},
        {"kind": "decision", "subject": "OK", "body": "fine"},
    ]))
    d = Distiller(conn, llm, "test-model", embedder=None)

    stats = await d.run_once()
    assert stats["facts_inserted"] == 1
    only = await conn.fetchrow("SELECT subject FROM project_facts")
    assert only["subject"] == "OK"


@pytest.mark.asyncio
async def test_llm_failure_leaves_rows_undistilled(pg_schema):
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)
    ids = await _seed_messages(conn, [("mf", "C1", "t1", "user", "decide", "1")])
    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(side_effect=RuntimeError("boom"))
    d = Distiller(conn, provider, "test-model", embedder=None)

    stats = await d.run_once()
    assert stats["errors"] == 1
    assert stats["facts_inserted"] == 0
    rows = await conn.fetch("SELECT distilled_at FROM messages WHERE id=ANY($1)", ids)
    assert all(r["distilled_at"] is None for r in rows)


def test_caps_facts_per_thread():
    # Pure logic check — no DB needed.
    payload = [{"kind": "fact", "subject": f"s{i}", "body": "b"} for i in range(_MAX_FACTS_PER_THREAD + 5)]
    parsed = Distiller._parse_facts(json.dumps(payload))
    assert len(parsed) == _MAX_FACTS_PER_THREAD


def test_normalize_subject_collapses_whitespace_and_case():
    assert _normalize_subject("  Auth   Approach ") == "auth approach"
    assert _normalize_subject("Auth\tApproach") == "auth approach"
