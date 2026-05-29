"""Append/read raw conversation messages (L1)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

import asyncpg

TOOL_BODY_MAX = 500


@dataclass
class AppendArgs:
    channel_type: str
    channel_id: str
    thread_ts: str
    project_id: Optional[str]
    user_id: Optional[str]
    role: str  # 'user' | 'assistant' | 'tool'
    body: str
    slack_ts: str
    tool_calls: Optional[dict[str, Any]] = None


class MessageStore:
    def __init__(self, conn: asyncpg.Connection | asyncpg.Pool):
        self._conn = conn

    async def append(self, a: AppendArgs) -> Optional[int]:
        body = a.body
        if a.role == "tool" and len(body) > TOOL_BODY_MAX:
            body = body[:TOOL_BODY_MAX]
        sql = """
        INSERT INTO messages
          (channel_type, channel_id, thread_ts, project_id, user_id,
           role, body, tool_calls, slack_ts)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
        ON CONFLICT (channel_type, channel_id, slack_ts) DO NOTHING
        RETURNING id
        """
        return await self._conn.fetchval(
            sql,
            a.channel_type,
            a.channel_id,
            a.thread_ts,
            a.project_id,
            a.user_id,
            a.role,
            body,
            json.dumps(a.tool_calls) if a.tool_calls else None,
            a.slack_ts,
        )
