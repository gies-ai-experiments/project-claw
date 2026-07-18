from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from uuid import UUID

import pytest

from nanobot.config.schema import Project
from nanobot.integrations.asana import AsanaResource, AsanaRetryableError
from nanobot.meeting_classifier.models import ApprovalSnapshot, ProjectDraft, TaskDraft
from nanobot.meeting_classifier.provisioning import ProvisioningWorker
from nanobot.meeting_classifier.repository import IdentityRecord, ProvisioningJob, ProvisioningStep


class Repo:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.job = ProvisioningJob(
            UUID("22222222-2222-2222-2222-222222222222"),
            snapshot.approval_id,
            "existing_project",
            "running",
        )
        self.claimed = False
        self.steps = {
            "000:project": ProvisioningStep("000:project", "pending", None),
            "001:task:t1": ProvisioningStep("001:task:t1", "pending", None),
        }
        self.completed = False
        self.released = False

    async def claim_next_job(self):
        if self.claimed:
            return None
        self.claimed = True
        return self.job

    async def get_snapshot(self, _job_id):
        return self.snapshot

    async def list_steps(self, _job_id):
        return list(self.steps.values())

    async def ensure_step(self, _job_id, name, _key):
        self.steps.setdefault(name, ProvisioningStep(name, "pending", None))

    async def mark_step_running(self, _job_id, name):
        step = self.steps[name]
        self.steps[name] = ProvisioningStep(name, "running", step.external_id)

    async def complete_step(self, _job_id, name, external_id):
        self.steps[name] = ProvisioningStep(name, "complete", external_id)

    async def fail_step(self, *_args, **_kwargs):
        return None

    async def complete_job(self, _job_id):
        self.completed = True

    async def release_retryable_job(self, _job_id, _retry_at):
        self.released = True

    async def recover_running_jobs(self):
        return 0


class Identities:
    async def get(self, email):
        values = {
            "ash@example.edu": ("U_ASH", "A_ASH"),
            "jordan@example.edu": ("U_JORDAN", "A_JORDAN"),
        }
        slack_id, asana_gid = values[email]
        return IdentityRecord(email, email, slack_id, asana_gid, None)


class Asana:
    def __init__(self):
        self.members = []
        self.parents = []
        self.subtasks = []
        self.followers = []
        self.fail_parent = False

    async def get_project(self, gid):
        return AsanaResource(gid, "Atlas")

    async def add_project_members(self, gid, users):
        self.members.append((gid, users))

    async def find_parent_task_by_marker(self, _project_gid, _marker):
        return None

    async def create_parent_task(self, project_gid, snapshot, marker):
        if self.fail_parent:
            raise AsanaRetryableError("safe", operation="POST", retry_after=2)
        self.parents.append((project_gid, snapshot.approval_id, marker))
        return AsanaResource("PARENT", "Meeting", permalink_url="https://app.asana.com/0/1")

    async def find_task_by_marker(self, _parent_gid, _marker):
        return None

    async def create_subtask(self, parent_gid, task, assignee_gid, marker):
        self.subtasks.append((parent_gid, task.id, assignee_gid, marker))
        return AsanaResource("SUB1", task.title)

    async def add_task_followers(self, task_gid, users):
        self.followers.append((task_gid, users))


class Slack:
    def __init__(self):
        self.posts = []

    async def find_message_by_marker(self, _channel, _marker):
        return None

    async def post_blocks(self, channel, text, blocks):
        self.posts.append(SimpleNamespace(channel_id=channel, text=text, blocks=blocks))
        return "10.1"

    async def open_dm(self, _user):
        return "DADMIN"


def snapshot():
    return ApprovalSnapshot(
        approval_id=UUID("11111111-1111-1111-1111-111111111111"),
        note_id="n1",
        meeting_title="Weekly",
        meeting_date=date(2026, 7, 18),
        revision=0,
        draft=ProjectDraft(
            project="atlas",
            summary="Approved work",
            tasks=[TaskDraft(
                id="t1", title="Ship", owner={"name": "Ash", "email": "ash@example.edu"},
                collaborators=[{"name": "Jordan", "email": "jordan@example.edu"}],
                due_on=date(2026, 7, 24), due_on_source="meeting",
            )],
        ),
    )


@pytest.mark.asyncio
async def test_existing_project_creates_tasks_followers_and_announcement() -> None:
    snap = snapshot()
    repo, asana, slack = Repo(snap), Asana(), Slack()
    worker = ProvisioningWorker(
        repo, asana, slack, Identities(),
        project_provider=lambda _name: Project(
            name="atlas", asana={"projectGid": "APROJ"}, channel="C_EXISTING"
        ),
        admin_slack_id="U_SAKSHI",
    )
    assert await worker.run_once() is True
    assert asana.parents[0][0] == "APROJ"
    assert asana.subtasks[0][2] == "A_ASH"
    assert asana.followers == [("SUB1", ["A_JORDAN"])]
    assert slack.posts[0].channel_id == "C_EXISTING"
    assert "https://app.asana.com/" in slack.posts[0].text
    assert repo.completed is True


@pytest.mark.asyncio
async def test_retryable_asana_failure_releases_same_job() -> None:
    snap = snapshot()
    repo, asana = Repo(snap), Asana()
    asana.fail_parent = True
    worker = ProvisioningWorker(
        repo, asana, Slack(), Identities(),
        project_provider=lambda _name: Project(
            name="atlas", asana={"projectGid": "APROJ"}, channel="C_EXISTING"
        ),
        admin_slack_id="U_SAKSHI",
    )
    assert await worker.run_once() is True
    assert repo.released is True
    assert repo.completed is False
