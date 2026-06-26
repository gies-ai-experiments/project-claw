"""L2 distiller — turns raw L1 messages into distilled per-project facts.

Runs on a schedule (registered as a system cron job at gateway boot — see
``cli/commands.py``). Each tick pulls undistilled L1 rows (``distilled_at IS
NULL``), groups them by ``(project_id, channel_id, thread_ts)``, asks an LLM to
extract structured facts (decision / action / fact / open_question / role),
embeds each fact's ``subject + ' ' + body`` for the hybrid search path, inserts
into ``project_facts``, supersedes older facts on the same project+kind+subject,
and finally marks the source L1 rows ``distilled_at = now()``.

The distiller is deliberately defensive: every LLM/parse/DB failure is logged
and the affected batch is skipped (its rows stay ``distilled_at IS NULL`` so the
next run retries them). A distiller tick never raises — it returns a stats
dict — so a cron-driven run can never abort the gateway.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import asyncpg
from loguru import logger

from nanobot.store.embeddings import to_pgvector

DISTILLER_VERSION = "v1"

_SYSTEM_PROMPT = (
    "You are a fact-distillation engine. You read a conversation transcript "
    "and extract durable per-project facts as a strict JSON array. Never "
    "include prose outside the JSON. If the thread has nothing durable, "
    "return []."
)

# Kinds that represent durable claims; newer facts on the same subject supersede
# older ones. Actions and open_questions are event-like — kept as-is.
_SUPERSEDED_KINDS = {"decision", "fact", "role"}
_VALID_KINDS = _SUPERSEDED_KINDS | {"action", "open_question"}

_MAX_FACTS_PER_THREAD = 25
_SUBJECT_MAX = 200
_BODY_MAX = 1000


@dataclass
class DistillStats:
    threads: int = 0
    messages_distilled: int = 0
    facts_inserted: int = 0
    facts_superseded: int = 0
    errors: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "threads": self.threads,
            "messages_distilled": self.messages_distilled,
            "facts_inserted": self.facts_inserted,
            "facts_superseded": self.facts_superseded,
            "errors": self.errors,
        }


@dataclass
class _ExtractedFact:
    kind: str
    subject: str
    body: str
    confidence: float = 1.0


@dataclass
class _PendingFact:
    extracted: _ExtractedFact
    embedding: list[float] | None = None
    db_id: int | None = None


class Distiller:
    """Distills L1 messages into L2 ``project_facts`` rows.

    The LLM provider is the standard ``LLMProvider.chat_with_retry`` interface
    (same as the agent loop), so any backend (OpenAI-compat, Anthropic, Bedrock,
    …) works. The ``embedder`` is optional — when ``None`` facts are inserted
    with ``embedding IS NULL`` and the search side falls back to FTS-only.
    """

    def __init__(
        self,
        conn: asyncpg.Connection | asyncpg.Pool,
        provider: Any,
        model: str,
        embedder: Any | None = None,
        *,
        distiller_version: str = DISTILLER_VERSION,
        batch_messages: int = 50,
        max_threads_per_run: int = 20,
    ) -> None:
        self._conn = conn
        self._provider = provider
        self._model = model
        self._embedder = embedder
        self._distiller_version = distiller_version
        self._batch_messages = batch_messages
        self._max_threads_per_run = max_threads_per_run

    # ------------------------------------------------------------------ API

    async def run_once(self) -> dict[str, int]:
        """Distill up to ``max_threads_per_run`` threads. Never raises."""
        stats = DistillStats()
        try:
            groups = await self._pick_undistilled()
        except Exception:
            logger.exception("distiller: failed to fetch undistilled messages")
            return stats.as_dict() | {"errors": 1}

        for (project_id, channel_id, thread_ts), rows in groups.items():
            try:
                facts = await self._distill_thread(project_id, channel_id, thread_ts, rows)
                if facts is None:
                    stats.errors += 1
                    continue
                inserted, superseded = await self._insert_facts(project_id, facts, [r["id"] for r in rows])
                await self._mark_distilled([r["id"] for r in rows])
                stats.threads += 1
                stats.messages_distilled += len(rows)
                stats.facts_inserted += inserted
                stats.facts_superseded += superseded
            except Exception:
                logger.exception(
                    "distiller: thread {}/{}/{} failed; leaving rows undistilled",
                    project_id, channel_id, thread_ts,
                )
                stats.errors += 1
        logger.info(
            "distiller: {} threads, {} messages, {} facts (+{} superseded), {} errors",
            stats.threads, stats.messages_distilled, stats.facts_inserted,
            stats.facts_superseded, stats.errors,
        )
        return stats.as_dict()

    # ----------------------------------------------------- L1 row selection

    async def _pick_undistilled(self) -> dict[tuple[str, str, str], list[asyncpg.Record]]:
        """Pick undistilled rows grouped by thread, oldest-first, up to budget.

        Returns groups in chronological order (oldest thread first) so a tick
        makes forward progress on the backlog rather than re-reading the tail.
        """
        rows = await self._conn.fetch(
            """
            SELECT id, project_id, channel_id, thread_ts, role, body, created_at
            FROM messages
            WHERE distilled_at IS NULL AND project_id IS NOT NULL
            ORDER BY created_at ASC, id ASC
            LIMIT $1
            """,
            self._batch_messages * 4,  # pull a chunk, then cap by thread count
        )
        groups: dict[tuple[str, str, str], list[asyncpg.Record]] = {}
        for r in rows:
            key = (r["project_id"], r["channel_id"], r["thread_ts"])
            groups.setdefault(key, []).append(r)
        if len(groups) > self._max_threads_per_run:
            # Preserve chronological order: keep the first N threads.
            ordered_keys = list(groups.keys())[: self._max_threads_per_run]
            groups = {k: groups[k] for k in ordered_keys}
        return groups

    # ---------------------------------------------------- LLM extraction

    async def _distill_thread(
        self,
        project_id: str,
        channel_id: str,
        thread_ts: str,
        rows: list[asyncpg.Record],
    ) -> list[_PendingFact] | None:
        if len(rows) > self._batch_messages:
            rows = rows[: self._batch_messages]
        transcript = self._render_transcript(rows)
        prompt = self._build_prompt(project_id, transcript)
        try:
            resp = await self._provider.chat_with_retry(
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                model=self._model,
                temperature=0.2,
            )
        except Exception:
            logger.exception("distiller: LLM call failed for {}/{}/{}", project_id, channel_id, thread_ts)
            return None
        content = (resp.content or "").strip() if resp.content else ""
        if not content or resp.finish_reason == "error":
            logger.warning("distiller: empty LLM response for {}/{}/{}", project_id, channel_id, thread_ts)
            return None
        extracted = self._parse_facts(content)
        if not extracted:
            logger.debug("distiller: no facts extracted for {}/{}/{}", project_id, channel_id, thread_ts)
            return []
        return await self._embed_facts(extracted)

    @staticmethod
    def _render_transcript(rows: list[asyncpg.Record]) -> str:
        lines: list[str] = []
        for r in rows:
            role = r["role"]
            body = r["body"]
            if role == "tool":
                lines.append(f"[tool] {body}")
            else:
                lines.append(f"[{role}] {body}")
        return "\n".join(lines)

    @staticmethod
    def _build_prompt(project_id: str, transcript: str) -> str:
        return (
            f"Project: {project_id}\n\n"
            "Transcript of a recent conversation thread:\n"
            "```\n"
            f"{transcript}\n"
            "```\n\n"
            "Extract durable facts a future agent working on this project would "
            "want to recall. Return ONLY a JSON array (no prose, no markdown "
            "fences). Each element MUST be an object with:\n"
            '- "kind": one of "decision", "action", "fact", "open_question", "role"\n'
            '- "subject": short label (<=200 chars)\n'
            '- "body": the fact itself (<=1000 chars)\n'
            '- "confidence": OPTIONAL float in [0,1] (default 1.0)\n\n'
            "Rules:\n"
            "- Skip greetings, chitchat, and one-off questions with no durable answer.\n"
            "- A decision is a choice that was made; a fact is something learned;\n"
            "  an open_question is unresolved; an action is a concrete step taken/assigned;\n"
            "  a role is who owns what.\n"
            "- If the thread has no durable content, return []."
        )

    @staticmethod
    def _parse_facts(content: str) -> list[_ExtractedFact]:
        """Parse the LLM response, tolerant of ```json fences and stray prose."""
        text = content.strip()
        # Strip ```json ... ``` or ``` ... ``` fences.
        fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
        if fence:
            text = fence.group(1).strip()
        # Find the first '[' ... matching up to the last ']' — robust against
        # trailing prose after the array.
        start = text.find("[")
        end = text.rfind("]")
        if start == -1 or end == -1 or end < start:
            return []
        payload = text[start:end + 1]
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return []
        if not isinstance(data, list):
            return []
        out: list[_ExtractedFact] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind", "")).strip().lower()
            if kind not in _VALID_KINDS:
                continue
            subject = str(item.get("subject", "")).strip()
            body = str(item.get("body", "")).strip()
            if not subject or not body:
                continue
            if len(subject) > _SUBJECT_MAX:
                subject = subject[:_SUBJECT_MAX]
            if len(body) > _BODY_MAX:
                body = body[:_BODY_MAX]
            conf_raw = item.get("confidence", 1.0)
            try:
                confidence = max(0.0, min(1.0, float(conf_raw)))
            except (TypeError, ValueError):
                confidence = 1.0
            out.append(_ExtractedFact(kind=kind, subject=subject, body=body, confidence=confidence))
            if len(out) >= _MAX_FACTS_PER_THREAD:
                break
        return out

    # ---------------------------------------------------- Embedding

    async def _embed_facts(self, facts: list[_ExtractedFact]) -> list[_PendingFact]:
        if not facts or self._embedder is None:
            return [_PendingFact(extracted=f) for f in facts]
        texts = [f"{f.subject} {f.body}" for f in facts]
        try:
            vecs = await self._embedder.embed(texts)
        except Exception:
            logger.exception("distiller: embedding failed; inserting facts without vectors")
            return [_PendingFact(extracted=f) for f in facts]
        out: list[_PendingFact] = []
        for f, vec in zip(facts, vecs, strict=False):
            out.append(_PendingFact(extracted=f, embedding=vec if vec else None))
        return out

    # ---------------------------------------------------- Insert + supersede

    async def _insert_facts(
        self,
        project_id: str,
        facts: list[_PendingFact],
        source_message_ids: list[int],
    ) -> tuple[int, int]:
        if not facts:
            return 0, 0
        source_arr = source_message_ids
        inserted = 0
        superseded = 0
        for f in facts:
            new_id = await self._conn.fetchval(
                """
                INSERT INTO project_facts
                  (project_id, kind, subject, body, source_message_ids,
                   confidence, distiller_version, embedding)
                VALUES ($1,$2,$3,$4,$5,$6,$7,
                        CASE WHEN $8::text IS NULL THEN NULL ELSE $8::vector END)
                RETURNING id
                """,
                project_id,
                f.extracted.kind,
                f.extracted.subject,
                f.extracted.body,
                source_arr,
                f.extracted.confidence,
                self._distiller_version,
                to_pgvector(f.embedding) if f.embedding else None,
            )
            if new_id is None:
                continue
            f.db_id = new_id
            inserted += 1
            if f.extracted.kind in _SUPERSEDED_KINDS:
                superseded += await self._supersede_older(project_id, f.extracted.kind, f.extracted.subject, new_id)
        return inserted, superseded

    async def _supersede_older(
        self, project_id: str, kind: str, subject: str, new_id: int
    ) -> int:
        """Mark prior current facts on the same (project, kind, normalized subject)
        as superseded by ``new_id``. Normalization keeps it predictable: match on
        lowercase + whitespace-collapsed subject.
        """
        norm = _normalize_subject(subject)
        result = await self._conn.execute(
            """
            UPDATE project_facts
            SET superseded_by = $1
            WHERE project_id = $2
              AND kind = $3
              AND superseded_by IS NULL
              AND id <> $1
              AND lower(regexp_replace(subject, '\\s+', ' ', 'g')) = $4
            """,
            new_id, project_id, kind, norm,
        )
        # asyncpg returns 'UPDATE N' on success.
        try:
            return int(result.split()[-1])
        except (ValueError, IndexError):
            return 0

    async def _mark_distilled(self, ids: list[int]) -> None:
        if not ids:
            return
        await self._conn.executemany(
            "UPDATE messages SET distilled_at = now() WHERE id = $1",
            [(i,) for i in ids],
        )


def _normalize_subject(subject: str) -> str:
    return re.sub(r"\s+", " ", subject.strip().lower())


__all__ = ["Distiller", "DistillStats", "DISTILLER_VERSION"]
