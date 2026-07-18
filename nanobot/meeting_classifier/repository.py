"""PostgreSQL repositories for meeting approvals and provisioning."""

from __future__ import annotations

import json
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal
from uuid import UUID, uuid4

import asyncpg

from nanobot.meeting_classifier.models import ApprovalSnapshot, ProjectDraft

Database = asyncpg.Connection | asyncpg.Pool


@asynccontextmanager
async def _acquire(database: Database):
    if isinstance(database, asyncpg.Pool):
        async with database.acquire() as conn:
            yield conn
    else:
        yield database


@dataclass(frozen=True)
class IdentityRecord:
    email: str
    display_name: str
    slack_user_id: str | None
    asana_user_gid: str | None
    verified_at: datetime | None


@dataclass(frozen=True)
class ApprovalRecord:
    id: UUID
    note_id: str
    revision: int
    status: str
    draft: ProjectDraft


@dataclass(frozen=True)
class ProvisioningJob:
    id: UUID
    approval_id: UUID
    kind: Literal["existing_project", "new_project"]
    status: str


@dataclass(frozen=True)
class ProvisioningStep:
    step_name: str
    status: str
    external_id: str | None


def _normalize_email(email: str) -> str:
    normalized = email.strip().lower()
    if not normalized:
        raise ValueError("email must not be blank")
    return normalized


def _json_value(value: object) -> object:
    if isinstance(value, str):
        return json.loads(value)
    return value


def _draft_json(draft: ProjectDraft) -> str:
    return draft.model_dump_json(by_alias=True)


def _approval_record(row: asyncpg.Record) -> ApprovalRecord:
    return ApprovalRecord(
        id=row["id"],
        note_id=row["note_id"],
        revision=row["revision"],
        status=row["status"],
        draft=ProjectDraft.model_validate(_json_value(row["draft"])),
    )


def _snapshot(row_value: object) -> ApprovalSnapshot:
    if row_value is None:
        raise ValueError("approval has no approved snapshot")
    return ApprovalSnapshot.model_validate(_json_value(row_value))


class IdentityRepository:
    def __init__(self, conn: Database) -> None:
        self._conn = conn

    async def get(self, email: str) -> IdentityRecord | None:
        row = await self._conn.fetchrow(
            "SELECT * FROM identity_directory WHERE email_normalized=$1",
            _normalize_email(email),
        )
        if row is None:
            return None
        return IdentityRecord(
            email=row["email_normalized"],
            display_name=row["display_name"],
            slack_user_id=row["slack_user_id"],
            asana_user_gid=row["asana_user_gid"],
            verified_at=row["verified_at"],
        )

    async def upsert_verified(self, record: IdentityRecord) -> None:
        await self._conn.execute(
            """
            INSERT INTO identity_directory
              (email_normalized, display_name, slack_user_id, asana_user_gid, verified_at)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (email_normalized) DO UPDATE SET
              display_name=EXCLUDED.display_name,
              slack_user_id=EXCLUDED.slack_user_id,
              asana_user_gid=EXCLUDED.asana_user_gid,
              verified_at=EXCLUDED.verified_at
            """,
            _normalize_email(record.email),
            record.display_name,
            record.slack_user_id,
            record.asana_user_gid,
            record.verified_at,
        )

    async def invalidate(self, email: str) -> None:
        await self._conn.execute(
            "UPDATE identity_directory SET verified_at=NULL WHERE email_normalized=$1",
            _normalize_email(email),
        )


class ApprovalRepository:
    def __init__(self, conn: Database) -> None:
        self._conn = conn

    async def create_draft(
        self,
        note_id: str,
        meeting_title: str,
        meeting_date: date,
        draft: ProjectDraft,
    ) -> ApprovalRecord:
        approval_id = uuid4()
        row = await self._conn.fetchrow(
            """
            INSERT INTO meeting_approval
              (id, note_id, meeting_title, meeting_date, project_key, status, draft)
            VALUES ($1, $2, $3, $4, $5, 'pending', $6::jsonb)
            ON CONFLICT (note_id, project_key) DO UPDATE
              SET note_id=EXCLUDED.note_id
            RETURNING *
            """,
            approval_id,
            note_id,
            meeting_title,
            meeting_date,
            draft.project,
            _draft_json(draft),
        )
        return _approval_record(row)

    async def get(self, approval_id: UUID) -> ApprovalRecord | None:
        row = await self._conn.fetchrow("SELECT * FROM meeting_approval WHERE id=$1", approval_id)
        return None if row is None else _approval_record(row)

    async def set_review_messages(
        self, approval_id: UUID, channel_id: str, message_ts: list[str]
    ) -> None:
        result = await self._conn.execute(
            """
            UPDATE meeting_approval
            SET review_channel_id=$2, review_message_ts=$3, updated_at=now()
            WHERE id=$1 AND status='pending'
            """,
            approval_id,
            channel_id,
            message_ts,
        )
        if result == "UPDATE 0":
            raise ValueError("approval is no longer pending")

    async def replace_draft(
        self, approval_id: UUID, draft: ProjectDraft, expected_revision: int
    ) -> ApprovalRecord:
        row = await self._conn.fetchrow(
            """
            UPDATE meeting_approval SET
              draft=$2::jsonb, project_key=$3, revision=revision+1, updated_at=now()
            WHERE id=$1 AND revision=$4 AND status='pending'
            RETURNING *
            """,
            approval_id,
            _draft_json(draft),
            draft.project,
            expected_revision,
        )
        if row is None:
            raise ValueError("stale revision or approval is no longer pending")
        return _approval_record(row)

    async def skip(self, approval_id: UUID, expected_revision: int) -> bool:
        result = await self._conn.execute(
            """
            UPDATE meeting_approval
            SET status='skipped', revision=revision+1, updated_at=now()
            WHERE id=$1 AND revision=$2 AND status='pending'
            """,
            approval_id,
            expected_revision,
        )
        return result == "UPDATE 1"

    async def approve_and_enqueue(
        self,
        approval_id: UUID,
        expected_revision: int,
        approver_slack_id: str,
    ) -> tuple[ApprovalSnapshot, UUID]:
        async with _acquire(self._conn) as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT * FROM meeting_approval WHERE id=$1 FOR UPDATE", approval_id
                )
                if row is None:
                    raise ValueError("unknown approval")
                if row["revision"] != expected_revision:
                    raise ValueError("stale approval revision")
                if row["status"] != "pending":
                    existing_job = await conn.fetchrow(
                        "SELECT id FROM provisioning_job WHERE approval_id=$1", approval_id
                    )
                    if row["approved_snapshot"] is not None and existing_job is not None:
                        return _snapshot(row["approved_snapshot"]), existing_job["id"]
                    raise ValueError("approval is no longer pending")

                draft = ProjectDraft.model_validate(_json_value(row["draft"]))
                snapshot = ApprovalSnapshot(
                    approval_id=approval_id,
                    note_id=row["note_id"],
                    meeting_title=row["meeting_title"],
                    meeting_date=row["meeting_date"],
                    revision=row["revision"],
                    draft=draft,
                )
                snapshot_json = snapshot.model_dump_json(by_alias=True)
                job_id = uuid4()
                kind = "new_project" if draft.is_new_project else "existing_project"
                await conn.execute(
                    """
                UPDATE meeting_approval SET
                  status='provisioning', approved_snapshot=$2::jsonb,
                  approver_slack_id=$3, approved_at=now(), updated_at=now()
                WHERE id=$1
                """,
                    approval_id,
                    snapshot_json,
                    approver_slack_id,
                )
                await conn.execute(
                    """
                INSERT INTO provisioning_job (id, approval_id, kind, status)
                VALUES ($1, $2, $3, 'pending')
                """,
                    job_id,
                    approval_id,
                    kind,
                )
                steps = [("000:project", f"approval:{approval_id}:project")]
                steps.extend(
                    (
                        f"{index:03d}:task:{task.id}",
                        f"approval:{approval_id}:task:{task.id}",
                    )
                    for index, task in enumerate(draft.tasks, start=1)
                )
                await conn.executemany(
                    """
                INSERT INTO provisioning_step
                  (job_id, step_name, status, idempotency_key)
                VALUES ($1, $2, 'pending', $3)
                """,
                    [(job_id, step_name, key) for step_name, key in steps],
                )
                return snapshot, job_id


_PRIVATE_KEY = re.compile(
    r"-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?-----END [^-\r\n]*PRIVATE KEY-----",
    re.IGNORECASE | re.DOTALL,
)
_SECRET_HEADER = re.compile(
    r"^(authorization|proxy-authorization|cookie|set-cookie)\s*:\s*.*$",
    re.IGNORECASE | re.MULTILINE,
)
_BEARER_CREDENTIAL = re.compile(r"\bbearer\s+[^\s,;]+", re.IGNORECASE)
_URL_USERINFO = re.compile(r"\b([a-z][a-z0-9+.-]*://)([^@\s/]+)@", re.IGNORECASE)
_SECRET_ASSIGNMENT = re.compile(
    r"""(?ix)
    (["']?(?:access[_-]?token|refresh[_-]?token|api[_-]?key|token|secret|password)["']?
    \s*[:=]\s*)
    (?:"[^"]*"|'[^']*'|[^,\s}\]]+)
    """,
)
_OPAQUE_TOKEN = re.compile(
    r"(?<![A-Za-z0-9._~+/=_-])"
    r"(?=[A-Za-z0-9._~+/=_-]{32,})(?=[A-Za-z0-9._~+/=_-]*[A-Za-z])"
    r"(?=[A-Za-z0-9._~+/=_-]*[0-9])[A-Za-z0-9._~+/=_-]{32,}"
    r"(?![A-Za-z0-9._~+/=_-])"
)


def _sanitize_error(value: str) -> str:
    safe = value.replace("\x00", " ")
    safe = _PRIVATE_KEY.sub("[redacted private key]", safe)
    safe = _SECRET_HEADER.sub(lambda match: f"{match.group(1)}: [redacted]", safe)
    safe = _BEARER_CREDENTIAL.sub("Bearer [redacted]", safe)
    safe = _URL_USERINFO.sub(r"\1[redacted]@", safe)
    safe = _SECRET_ASSIGNMENT.sub(r"\1[redacted]", safe)
    safe = _OPAQUE_TOKEN.sub("[redacted]", safe)
    return safe[:1000]


class ProvisioningRepository:
    def __init__(self, conn: Database) -> None:
        self._conn = conn

    async def claim_next_job(self) -> ProvisioningJob | None:
        async with _acquire(self._conn) as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                SELECT * FROM provisioning_job
                WHERE status='pending' AND (retry_at IS NULL OR retry_at <= now())
                ORDER BY created_at, id
                FOR UPDATE SKIP LOCKED
                LIMIT 1
                """
                )
                if row is None:
                    return None
                updated = await conn.fetchrow(
                    """
                UPDATE provisioning_job
                SET status='running', retry_at=NULL, updated_at=now()
                WHERE id=$1 RETURNING *
                """,
                    row["id"],
                )
                await conn.execute(
                    """
                UPDATE meeting_approval SET status='provisioning', updated_at=now()
                WHERE id=$1
                """,
                    row["approval_id"],
                )
                return ProvisioningJob(
                    id=updated["id"],
                    approval_id=updated["approval_id"],
                    kind=updated["kind"],
                    status=updated["status"],
                )

    async def get_snapshot(self, job_id: UUID) -> ApprovalSnapshot:
        value = await self._conn.fetchval(
            """
            SELECT a.approved_snapshot
            FROM provisioning_job j JOIN meeting_approval a ON a.id=j.approval_id
            WHERE j.id=$1
            """,
            job_id,
        )
        return _snapshot(value)

    async def list_steps(self, job_id: UUID) -> list[ProvisioningStep]:
        rows = await self._conn.fetch(
            """
            SELECT step_name, status, external_id
            FROM provisioning_step
            WHERE job_id=$1
            ORDER BY step_name
            """,
            job_id,
        )
        return [
            ProvisioningStep(row["step_name"], row["status"], row["external_id"])
            for row in rows
        ]

    async def ensure_step(self, job_id: UUID, step_name: str, idempotency_key: str) -> None:
        await self._conn.execute(
            """
            INSERT INTO provisioning_step
              (job_id, step_name, status, idempotency_key)
            VALUES ($1, $2, 'pending', $3)
            ON CONFLICT (job_id, step_name) DO NOTHING
            """,
            job_id,
            step_name,
            idempotency_key,
        )

    async def mark_step_running(self, job_id: UUID, step_name: str) -> None:
        result = await self._conn.execute(
            """
            UPDATE provisioning_step
            SET status='running', updated_at=now()
            WHERE job_id=$1 AND step_name=$2 AND status IN ('pending', 'running')
            """,
            job_id,
            step_name,
        )
        if result == "UPDATE 0":
            raise ValueError("unknown or completed provisioning step")

    async def complete_step(self, job_id: UUID, step_name: str, external_id: str | None) -> None:
        result = await self._conn.execute(
            """
            UPDATE provisioning_step SET
              status='complete', external_id=COALESCE(external_id, $3),
              attempt_count=CASE WHEN status='complete' THEN attempt_count
                                 ELSE attempt_count+1 END,
              last_error=NULL, updated_at=now()
            WHERE job_id=$1 AND step_name=$2
            """,
            job_id,
            step_name,
            external_id,
        )
        if result == "UPDATE 0":
            raise ValueError("unknown provisioning step")

    async def fail_step(
        self, job_id: UUID, step_name: str, safe_error: str, permanent: bool
    ) -> None:
        status = "needs_attention" if permanent else "pending"
        async with _acquire(self._conn) as conn:
            async with conn.transaction():
                result = await conn.execute(
                    """
                UPDATE provisioning_step SET
                  status=$3, attempt_count=attempt_count+1, last_error=$4, updated_at=now()
                WHERE job_id=$1 AND step_name=$2 AND status <> 'complete'
                """,
                    job_id,
                    step_name,
                    status,
                    _sanitize_error(safe_error),
                )
                if result == "UPDATE 0":
                    raise ValueError("unknown or already completed provisioning step")
                if permanent:
                    approval_id = await conn.fetchval(
                        """
                    UPDATE provisioning_job SET status='needs_attention', updated_at=now()
                    WHERE id=$1 RETURNING approval_id
                    """,
                        job_id,
                    )
                    await conn.execute(
                        """
                    UPDATE meeting_approval SET status='needs_attention', updated_at=now()
                    WHERE id=$1
                    """,
                        approval_id,
                    )

    async def complete_job(self, job_id: UUID) -> None:
        async with _acquire(self._conn) as conn:
            async with conn.transaction():
                incomplete = await conn.fetchval(
                    """
                SELECT COUNT(*) FROM provisioning_step
                WHERE job_id=$1 AND status <> 'complete'
                """,
                    job_id,
                )
                if incomplete:
                    raise ValueError("cannot complete a job with incomplete steps")
                approval_id = await conn.fetchval(
                    """
                UPDATE provisioning_job SET status='complete', retry_at=NULL, updated_at=now()
                WHERE id=$1 RETURNING approval_id
                """,
                    job_id,
                )
                if approval_id is None:
                    raise ValueError("unknown provisioning job")
                await conn.execute(
                    """
                UPDATE meeting_approval SET status='complete', updated_at=now()
                WHERE id=$1
                """,
                    approval_id,
                )

    async def recover_running_jobs(self) -> int:
        """Make work abandoned by this deployment's prior worker claimable again."""
        async with _acquire(self._conn) as conn:
            async with conn.transaction():
                job_ids = await conn.fetch(
                    "SELECT id FROM provisioning_job WHERE status='running' FOR UPDATE"
                )
                if not job_ids:
                    return 0
                ids = [row["id"] for row in job_ids]
                await conn.execute(
                    """
                    UPDATE provisioning_step SET status='pending', updated_at=now()
                    WHERE job_id=ANY($1::uuid[]) AND status='running'
                    """,
                    ids,
                )
                await conn.execute(
                    """
                    UPDATE provisioning_job
                    SET status='pending', retry_at=NULL, updated_at=now()
                    WHERE id=ANY($1::uuid[])
                    """,
                    ids,
                )
                return len(ids)

    async def release_retryable_job(self, job_id: UUID, retry_at: datetime) -> None:
        result = await self._conn.execute(
            """
            UPDATE provisioning_job
            SET status='pending', retry_at=$2, updated_at=now()
            WHERE id=$1 AND status='running'
            """,
            job_id,
            retry_at,
        )
        if result == "UPDATE 0":
            raise ValueError("job is not running")

    async def retry_needs_attention(self, approval_id: UUID) -> bool:
        """Resume the same immutable snapshot after Sakshi explicitly retries."""
        async with _acquire(self._conn) as conn:
            async with conn.transaction():
                job_id = await conn.fetchval(
                    """
                    UPDATE provisioning_job
                    SET status='pending', retry_at=NULL, updated_at=now()
                    WHERE approval_id=$1 AND status='needs_attention'
                    RETURNING id
                    """,
                    approval_id,
                )
                if job_id is None:
                    return False
                await conn.execute(
                    """
                    UPDATE provisioning_step
                    SET status='pending', last_error=NULL, updated_at=now()
                    WHERE job_id=$1 AND status='needs_attention'
                    """,
                    job_id,
                )
                await conn.execute(
                    """
                    UPDATE meeting_approval
                    SET status='provisioning', updated_at=now()
                    WHERE id=$1
                    """,
                    approval_id,
                )
                return True
