from __future__ import annotations

import asyncio
import os
from datetime import date, datetime, timedelta, timezone

import asyncpg
import pytest

from nanobot.meeting_classifier.models import PersonRef, ProjectDraft, TaskDraft
from nanobot.meeting_classifier.repository import (
    ApprovalRepository,
    IdentityRecord,
    IdentityRepository,
    ProvisioningRepository,
)
from nanobot.store.migrations import apply_migrations


def _draft(project: str = "atlas", *, new: bool = False) -> ProjectDraft:
    values = {
        "project": project,
        "summary": "Approved work",
        "tasks": [TaskDraft(id="t1", title="Ship sync")],
    }
    if new:
        values.update(
            is_new_project=True,
            display_name="Atlas Next",
            description="Next generation Atlas",
            channel_slug="atlas-next",
            lead=PersonRef(name="Lead", email="lead@example.edu"),
        )
    return ProjectDraft(**values)


@pytest.mark.asyncio
async def test_identity_keys_are_normalized_and_invalidation_preserves_identity(pg_schema):
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)
    repo = IdentityRepository(conn)
    verified_at = datetime.now(timezone.utc)
    record = IdentityRecord(" Sakshi@Example.EDU ", "Sakshi", "U1", "A1", verified_at)

    await repo.upsert_verified(record)
    assert await repo.get("SAKSHI@example.edu") == IdentityRecord(
        "sakshi@example.edu", "Sakshi", "U1", "A1", verified_at
    )
    await repo.invalidate(" SAKSHI@example.edu ")
    invalidated = await repo.get("sakshi@example.edu")
    assert invalidated is not None
    assert invalidated.verified_at is None
    assert invalidated.slack_user_id == "U1"


@pytest.mark.asyncio
async def test_approval_revision_transitions_reject_stale_writers(pg_schema):
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)
    repo = ApprovalRepository(conn)
    approval = await repo.create_draft("n1", "Sync", date(2026, 7, 17), _draft())
    assert approval.status == "pending"

    changed = await repo.replace_draft(
        approval.id,
        _draft().model_copy(update={"summary": "Reviewed"}),
        expected_revision=0,
    )
    assert changed.revision == 1
    with pytest.raises(ValueError, match="stale"):
        await repo.replace_draft(approval.id, _draft(), expected_revision=0)
    assert await repo.skip(approval.id, expected_revision=0) is False
    assert await repo.skip(approval.id, expected_revision=1) is True


@pytest.mark.asyncio
async def test_one_logical_approval_per_note_and_project(pg_schema):
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)
    repo = ApprovalRepository(conn)
    first = await repo.create_draft("n1", "Sync", date(2026, 7, 17), _draft())
    second = await repo.create_draft("n1", "Renamed", date(2026, 7, 18), _draft())
    assert second.id == first.id
    assert await conn.fetchval("SELECT COUNT(*) FROM meeting_approval") == 1


@pytest.mark.asyncio
async def test_approve_and_enqueue_is_atomic_and_idempotent(pg_schema):
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)
    repo = ApprovalRepository(conn)
    approval = await repo.create_draft("n1", "Sync", date(2026, 7, 17), _draft())

    snapshot, job_id = await repo.approve_and_enqueue(approval.id, 0, "U_SAKSHI")
    assert snapshot.revision == 0
    assert (
        await conn.fetchval(
            "SELECT COUNT(*) FROM provisioning_job WHERE approval_id=$1", approval.id
        )
        == 1
    )
    steps = await conn.fetch(
        "SELECT step_name, idempotency_key FROM provisioning_step "
        "WHERE job_id=$1 ORDER BY step_name",
        job_id,
    )
    assert len(steps) == 2
    assert len({row["idempotency_key"] for row in steps}) == 2

    same_snapshot, same_job_id = await repo.approve_and_enqueue(approval.id, 0, "U_SAKSHI")
    assert same_snapshot == snapshot
    assert same_job_id == job_id


@pytest.mark.asyncio
async def test_approve_and_enqueue_rolls_back_partial_failure(pg_schema):
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)
    repo = ApprovalRepository(conn)
    approval = await repo.create_draft("n1", "Sync", date(2026, 7, 17), _draft())
    await conn.execute(
        "ALTER TABLE provisioning_step ADD CONSTRAINT reject_all_steps CHECK (false)"
    )

    with pytest.raises(asyncpg.CheckViolationError):
        await repo.approve_and_enqueue(approval.id, 0, "U_SAKSHI")

    row = await conn.fetchrow("SELECT * FROM meeting_approval WHERE id=$1", approval.id)
    assert row["status"] == "pending"
    assert row["approved_snapshot"] is None
    assert await conn.fetchval("SELECT COUNT(*) FROM provisioning_job") == 0


@pytest.mark.asyncio
async def test_pool_approve_and_enqueue_rolls_back_partial_failure(pg_schema):
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)

    async def setup(pool_conn):
        await pool_conn.execute(f'SET search_path TO "{schema}", public')

    pool = await asyncpg.create_pool(
        os.environ["PROJECTCLAW_TEST_PG_DSN"], min_size=1, max_size=2, setup=setup
    )
    try:
        repo = ApprovalRepository(pool)
        approval = await repo.create_draft(
            "pool-rollback", "Sync", date(2026, 7, 17), _draft()
        )
        await pool.execute(
            "ALTER TABLE provisioning_step ADD CONSTRAINT reject_pool_steps CHECK (false)"
        )

        with pytest.raises(asyncpg.CheckViolationError):
            await repo.approve_and_enqueue(approval.id, 0, "U_SAKSHI")

        row = await pool.fetchrow("SELECT * FROM meeting_approval WHERE id=$1", approval.id)
        assert row["status"] == "pending"
        assert row["approved_snapshot"] is None
        assert await pool.fetchval("SELECT COUNT(*) FROM provisioning_job") == 0
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_approved_snapshot_is_immutable(pg_schema):
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)
    approvals = ApprovalRepository(conn)
    approval = await approvals.create_draft("n1", "Sync", date(2026, 7, 17), _draft())
    snapshot, job_id = await approvals.approve_and_enqueue(approval.id, 0, "U1")
    await conn.execute(
        "UPDATE meeting_approval SET draft=$2::jsonb WHERE id=$1",
        approval.id,
        _draft().model_copy(update={"summary": "Changed later"}).model_dump_json(by_alias=True),
    )

    assert (await ProvisioningRepository(conn).get_snapshot(job_id)) == snapshot
    with pytest.raises(ValueError):
        await approvals.replace_draft(approval.id, _draft(), expected_revision=0)


@pytest.mark.asyncio
async def test_provisioning_lifecycle_and_safe_failure_storage(pg_schema):
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)
    approvals = ApprovalRepository(conn)
    approval = await approvals.create_draft("n1", "Sync", date(2026, 7, 17), _draft())
    _, job_id = await approvals.approve_and_enqueue(approval.id, 0, "U1")
    repo = ProvisioningRepository(conn)

    claimed = await repo.claim_next_job()
    assert claimed is not None and claimed.id == job_id and claimed.status == "running"
    await repo.complete_step(job_id, "000:project", "P1")
    await repo.fail_step(job_id, "001:task:t1", "safe diagnostic", permanent=False)
    retry_at = datetime.now(timezone.utc) + timedelta(minutes=2)
    await repo.release_retryable_job(job_id, retry_at)
    assert await repo.claim_next_job() is None
    await conn.execute("UPDATE provisioning_job SET retry_at=now() - interval '1 second'")
    assert (await repo.claim_next_job()).id == job_id
    await repo.complete_step(job_id, "001:task:t1", "T1")
    await repo.complete_job(job_id)
    assert (
        await conn.fetchval("SELECT status FROM meeting_approval WHERE id=$1", approval.id)
        == "complete"
    )


@pytest.mark.asyncio
async def test_concurrent_workers_claim_distinct_jobs(pg_schema):
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)
    approvals = ApprovalRepository(conn)
    for number in (1, 2):
        approval = await approvals.create_draft(
            f"n{number}", "Sync", date(2026, 7, 17), _draft(f"atlas-{number}")
        )
        await approvals.approve_and_enqueue(approval.id, 0, "U1")

    second = await asyncpg.connect(os.environ["PROJECTCLAW_TEST_PG_DSN"])
    try:
        await second.execute(f'SET search_path TO "{schema}", public')
        claims = await asyncio.gather(
            ProvisioningRepository(conn).claim_next_job(),
            ProvisioningRepository(second).claim_next_job(),
        )
    finally:
        await second.close()
    assert None not in claims
    assert claims[0].id != claims[1].id


@pytest.mark.asyncio
async def test_pool_transactions_and_crash_recovery_preserve_completed_work(pg_schema):
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)

    async def setup(pool_conn):
        await pool_conn.execute(f'SET search_path TO "{schema}", public')

    pool = await asyncpg.create_pool(
        os.environ["PROJECTCLAW_TEST_PG_DSN"], min_size=1, max_size=3, setup=setup
    )
    try:
        approvals = ApprovalRepository(pool)
        approval = await approvals.create_draft("pool-note", "Sync", date(2026, 7, 17), _draft())
        _, job_id = await approvals.approve_and_enqueue(approval.id, 0, "U1")
        provisioning = ProvisioningRepository(pool)
        assert (await provisioning.claim_next_job()).id == job_id
        await provisioning.complete_step(job_id, "001:task:t1", "TASK-EXTERNAL")
        await provisioning.fail_step(
            job_id,
            "000:project",
            "temporary safe diagnostic",
            permanent=False,
        )
        await pool.execute(
            "UPDATE provisioning_step SET status='running' "
            "WHERE job_id=$1 AND step_name='000:project'",
            job_id,
        )

        assert await provisioning.recover_running_jobs() == 1
        rows = await pool.fetch(
            "SELECT step_name, status, external_id, attempt_count FROM provisioning_step "
            "WHERE job_id=$1 ORDER BY step_name",
            job_id,
        )
        assert dict(rows[0]) == {
            "step_name": "000:project",
            "status": "pending",
            "external_id": None,
            "attempt_count": 1,
        }
        assert dict(rows[1]) == {
            "step_name": "001:task:t1",
            "status": "complete",
            "external_id": "TASK-EXTERNAL",
            "attempt_count": 1,
        }
        assert (await provisioning.claim_next_job()).id == job_id
        await provisioning.complete_step(job_id, "000:project", "PROJECT-EXTERNAL")
        await provisioning.complete_job(job_id)
        assert (
            await pool.fetchval("SELECT status FROM meeting_approval WHERE id=$1", approval.id)
            == "complete"
        )

        permanent = await approvals.create_draft(
            "pool-note-permanent",
            "Sync",
            date(2026, 7, 17),
            _draft("atlas-permanent"),
        )
        _, permanent_job_id = await approvals.approve_and_enqueue(permanent.id, 0, "U1")
        assert (await provisioning.claim_next_job()).id == permanent_job_id
        await provisioning.fail_step(
            permanent_job_id,
            "000:project",
            "safe permanent diagnostic",
            permanent=True,
        )
        assert await pool.fetchval(
            "SELECT status FROM provisioning_job WHERE id=$1", permanent_job_id
        ) == "needs_attention"
        assert await pool.fetchval(
            "SELECT status FROM meeting_approval WHERE id=$1", permanent.id
        ) == "needs_attention"
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_failure_storage_redacts_obvious_secret_forms(pg_schema):
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)
    approvals = ApprovalRepository(conn)
    approval = await approvals.create_draft("secrets", "Sync", date(2026, 7, 17), _draft())
    _, job_id = await approvals.approve_and_enqueue(approval.id, 0, "U1")
    provisioning = ProvisioningRepository(conn)
    sentinels = [
        "AUTH_SENTINEL_12345678901234567890",
        "BEARER_SHORT_SENTINEL",
        "COOKIE_SENTINEL_123456789012345678",
        "JSON_SENTINEL_12345678901234567890",
        "ASSIGN_SENTINEL_123456789012345678",
        "DB_DSN_SENTINEL_123456789012345678",
        "HTTP_DSN_SENTINEL_1234567890123456",
        "PEM_SENTINEL_123456789012345678901",
        "OPAQUE_SENTINEL_123456789012345678901234567890",
    ]
    raw = "\n".join(
        [
            "request rejected safely",
            f"Authorization: Bearer {sentinels[0]}",
            f"Bearer {sentinels[1]}",
            f"Cookie: session={sentinels[2]}",
            f'{{"access_token": "{sentinels[3]}"}}',
            f"api_key={sentinels[4]}",
            f"postgresql://user:{sentinels[5]}@db.example/test",
            f"https://user:{sentinels[6]}@service.example/path",
            "-----BEGIN PRIVATE KEY-----\n" + sentinels[7] + "\n-----END PRIVATE KEY-----",
            sentinels[8],
        ]
    )

    await provisioning.fail_step(job_id, "000:project", raw, permanent=False)
    persisted = await conn.fetchval(
        "SELECT last_error FROM provisioning_step WHERE job_id=$1 AND step_name='000:project'",
        job_id,
    )
    assert "request rejected safely" in persisted
    assert "[redacted]" in persisted
    assert len(persisted) <= 1000
    for sentinel in sentinels:
        assert sentinel not in persisted
