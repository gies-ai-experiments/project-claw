from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from uuid import UUID

import pytest

from nanobot.integrations.asana import AsanaResource
from nanobot.integrations.slack_workspace import SlackResource
from nanobot.meeting_classifier.models import ApprovalSnapshot, ProjectDraft, TaskDraft
from nanobot.meeting_classifier.provisioning import ProvisioningWorker
from nanobot.meeting_classifier.repository import IdentityRecord, ProvisioningJob, ProvisioningStep


class Repo:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.claimed = False
        self.completed = False
        self.steps = {
            "000:project": ProvisioningStep("000:project", "pending", None),
            "001:task:t1": ProvisioningStep("001:task:t1", "pending", None),
        }
        self.job = ProvisioningJob(
            UUID("22222222-2222-2222-2222-222222222222"),
            snapshot.approval_id,
            "new_project",
            "running",
        )

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


class Identities:
    async def get(self, email):
        values = {
            "lead@example.edu": ("U_LEAD", "A_LEAD"),
            "ash@example.edu": ("U_ASH", "A_ASH"),
        }
        slack_id, asana_gid = values[email]
        return IdentityRecord(email, email, slack_id, asana_gid, None)


class Asana:
    def __init__(self):
        self.created_projects = []
        self.members = []
        self.owner = None
        self.subtasks = []

    async def find_project_by_marker(self, _name, _marker):
        return None

    async def create_project(self, *, name, notes, marker):
        self.created_projects.append((name, notes, marker))
        return AsanaResource("A_PROJECT", name, permalink_url="https://app.asana.com/0/p")

    async def add_project_members(self, _gid, users):
        self.members.extend(users)

    async def set_project_owner(self, _gid, user):
        self.owner = user

    async def find_parent_task_by_marker(self, _gid, _marker):
        return None

    async def create_parent_task(self, _gid, _snapshot, _marker):
        return AsanaResource("PARENT", "Meeting", permalink_url="https://app.asana.com/0/t")

    async def find_task_by_marker(self, _parent, _marker):
        return None

    async def create_subtask(self, parent, task, assignee, marker):
        self.subtasks.append((parent, task.id, assignee, marker))
        return AsanaResource("SUBTASK", task.title)

    async def add_task_followers(self, _gid, _users):
        return None


class Slack:
    def __init__(self):
        self.created_channels = []
        self.invited_users = []
        self.posts = []

    async def find_channel_by_slug(self, _slug):
        return None

    async def create_public_channel(self, slug):
        self.created_channels.append(slug)
        return SlackResource("C_NEW", slug)

    async def set_channel_marker(self, _channel, _marker):
        return None

    async def invite_users(self, _channel, users):
        self.invited_users.extend(users)

    async def find_message_by_marker(self, _channel, _marker):
        return None

    async def post_blocks(self, channel, text, blocks):
        self.posts.append(SimpleNamespace(channel_id=channel, text=text, blocks=blocks))
        return "10.1"


class Registry:
    def __init__(self):
        self.reserved = []
        self.activated = []

    async def reserve_new_project(self, draft, approver):
        self.reserved.append((draft.project, draft.channel_slug, approver))

    async def activate_dynamic(self, project, channel, asana):
        self.activated.append((project, channel, asana))


class LiveSlack:
    def __init__(self):
        self.projects = []

    def activate_project(self, project, channel):
        self.projects.append((project, channel))


@pytest.mark.asyncio
async def test_new_project_provisions_resources_members_and_runtime_mapping() -> None:
    snapshot = ApprovalSnapshot(
        approval_id=UUID("11111111-1111-1111-1111-111111111111"),
        note_id="n1",
        meeting_title="Kickoff",
        meeting_date=date(2026, 7, 18),
        revision=0,
        draft=ProjectDraft(
            project="new-lab",
            is_new_project=True,
            display_name="New Lab",
            description="A research project",
            channel_slug="new-lab",
            lead={"name": "Lead", "email": "lead@example.edu"},
            tasks=[TaskDraft(
                id="t1",
                title="Ship",
                owner={"name": "Ash", "email": "ash@example.edu"},
            )],
        ),
    )
    repo, asana, slack, registry, live = Repo(snapshot), Asana(), Slack(), Registry(), LiveSlack()
    worker = ProvisioningWorker(
        repo,
        asana,
        slack,
        Identities(),
        project_provider=lambda _name: None,
        admin_slack_id="U_SAKSHI",
        registry=registry,
        slack_channel=live,
    )

    assert await worker.run_once() is True
    assert asana.created_projects[0][0] == "New Lab"
    assert asana.owner == "A_LEAD"
    assert set(asana.members) == {"A_LEAD", "A_ASH"}
    assert slack.created_channels == ["new-lab"]
    assert set(slack.invited_users) == {"U_LEAD", "U_ASH"}
    assert live.projects[0][0].name == "new-lab"
    assert registry.activated[0][1:] == ("C_NEW", "A_PROJECT")
    assert repo.completed is True
