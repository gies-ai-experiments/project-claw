from datetime import UTC, date, datetime

import pytest

from nanobot.config.schema import Project
from nanobot.integrations.asana import AsanaResource
from nanobot.meeting_classifier.models import ProjectDraft, TaskDraft
from nanobot.meeting_classifier.provisioning import ProvisioningWorker
from nanobot.meeting_classifier.repository import (
    ApprovalRepository,
    IdentityRecord,
    IdentityRepository,
    ProvisioningRepository,
)
from nanobot.store.migrations import apply_migrations


class Asana:
    def __init__(self) -> None:
        self.parents: list[str] = []
        self.subtasks: list[tuple[str, str | None]] = []

    async def get_project(self, gid):
        return AsanaResource(gid, "Atlas")

    async def add_project_members(self, _gid, _users):
        return None

    async def find_parent_task_by_marker(self, _gid, _marker):
        return None

    async def create_parent_task(self, _gid, _snapshot, marker):
        self.parents.append(marker)
        return AsanaResource("PARENT", "Meeting", permalink_url="https://app.asana.com/0/p")

    async def find_task_by_marker(self, _parent, _marker):
        return None

    async def create_subtask(self, _parent, task, assignee, _marker):
        self.subtasks.append((task.id, assignee))
        return AsanaResource(f"TASK-{task.id}", task.title)

    async def add_task_followers(self, _gid, _users):
        return None


class Slack:
    def __init__(self) -> None:
        self.posts: list[str] = []

    async def find_message_by_marker(self, _channel, _marker):
        return None

    async def post_blocks(self, _channel, text, _blocks):
        self.posts.append(text)
        return "10.1"


@pytest.mark.asyncio
async def test_approved_snapshot_runs_from_postgres_to_asana_and_slack(pg_schema) -> None:
    schema, conn = pg_schema
    await apply_migrations(conn, schema=schema)
    identities = IdentityRepository(conn)
    await identities.upsert_verified(IdentityRecord(
        "ashley@example.edu", "Ashley", "U_ASHLEY", "A_ASHLEY", datetime.now(UTC)
    ))
    approvals = ApprovalRepository(conn)
    record = await approvals.create_draft(
        "note-1",
        "Weekly",
        date(2026, 7, 18),
        ProjectDraft(
            project="atlas",
            summary="Approved work",
            tasks=[TaskDraft(
                id="ship",
                title="Ship it",
                owner={"name": "Ashley", "email": "ashley@example.edu"},
                due_on=date(2026, 7, 24),
                due_on_source="meeting",
            )],
        ),
    )
    _snapshot, job_id = await approvals.approve_and_enqueue(record.id, 0, "U_SAKSHI")
    asana, slack = Asana(), Slack()
    worker = ProvisioningWorker(
        ProvisioningRepository(conn),
        asana,
        slack,
        identities,
        project_provider=lambda _name: Project(
            name="atlas", asana={"projectGid": "A_PROJECT"}, channel="C_ATLAS"
        ),
        admin_slack_id="U_SAKSHI",
    )

    assert await worker.run_once() is True
    assert len(asana.parents) == 1
    assert asana.subtasks == [("ship", "A_ASHLEY")]
    assert len(slack.posts) == 1
    assert await conn.fetchval(
        "SELECT status FROM provisioning_job WHERE id=$1", job_id
    ) == "complete"
