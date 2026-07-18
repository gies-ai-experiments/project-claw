from types import SimpleNamespace
from uuid import UUID

import pytest

from nanobot.meeting_classifier.coordinator import MeetingApprovalCoordinator
from nanobot.meeting_classifier.models import ProjectDraft, TaskDraft


class Repo:
    def __init__(self, draft):
        self.record = SimpleNamespace(
            id=UUID("11111111-1111-1111-1111-111111111111"),
            note_id="n1",
            revision=0,
            status="pending",
            draft=draft,
        )
        self.approved = False

    async def get(self, _approval_id):
        return self.record

    async def approve_and_enqueue(self, approval_id, expected_revision, approver_slack_id):
        assert expected_revision == 0 and approver_slack_id == "U_SAKSHI"
        self.approved = True
        return SimpleNamespace(approval_id=approval_id), UUID("22222222-2222-2222-2222-222222222222")


class Identity:
    async def resolve(self, person):
        if person.email == "missing@example.edu":
            raise ValueError("missing")
        return SimpleNamespace(email=person.email, slack_user_id="U1", asana_user_gid="A1")


@pytest.mark.asyncio
async def test_coordinator_is_admin_only_and_blocks_unresolved_identity() -> None:
    draft = ProjectDraft(
        project="atlas",
        tasks=[TaskDraft(id="t1", title="Ship", owner={"name": "Missing", "email": "missing@example.edu"})],
    )
    coordinator = MeetingApprovalCoordinator(
        Repo(draft), None, Identity(), None, admin_slack_id="U_SAKSHI"
    )
    approval_id = "11111111-1111-1111-1111-111111111111"
    denied = await coordinator.handle_interaction({
        "type": "block_actions", "user": {"id": "U_OTHER"},
        "actions": [{"value": f"mtg2:approve:{approval_id}:0:-"}],
    })
    assert denied.kind == "forbidden"
    result = await coordinator.handle_interaction({
        "type": "block_actions", "user": {"id": "U_SAKSHI"},
        "actions": [{"value": f"mtg2:approve:{approval_id}:0:-"}],
    })
    assert result.kind == "validation_error"
    assert "missing@example.edu" in result.message


@pytest.mark.asyncio
async def test_coordinator_approves_valid_snapshot_atomically() -> None:
    draft = ProjectDraft(project="atlas", tasks=[TaskDraft(id="t1", title="Ship")])
    repo = Repo(draft)
    coordinator = MeetingApprovalCoordinator(
        repo, None, Identity(), None, admin_slack_id="U_SAKSHI"
    )
    result = await coordinator.handle_interaction({
        "type": "block_actions", "user": {"id": "U_SAKSHI"},
        "actions": [{"value": "mtg2:approve:11111111-1111-1111-1111-111111111111:0:-"}],
    })
    assert result.kind == "approved"
    assert repo.approved is True


@pytest.mark.asyncio
async def test_coordinator_reads_live_project_names_at_approval_time() -> None:
    draft = ProjectDraft(project="dynamic", tasks=[TaskDraft(id="t1", title="Ship")])
    repo = Repo(draft)
    projects = {"atlas"}
    coordinator = MeetingApprovalCoordinator(
        repo,
        None,
        Identity(),
        None,
        admin_slack_id="U_SAKSHI",
        known_projects=lambda: projects,
    )
    projects.add("dynamic")
    result = await coordinator.handle_interaction({
        "type": "block_actions",
        "user": {"id": "U_SAKSHI"},
        "actions": [{
            "value": "mtg2:approve:11111111-1111-1111-1111-111111111111:0:-"
        }],
    })
    assert result.kind == "approved"
