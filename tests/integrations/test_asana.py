import json
from datetime import date
from uuid import UUID

import httpx
import pytest

from nanobot.config.schema import AsanaIntegrationConfig
from nanobot.integrations.asana import (
    AsanaAmbiguousError,
    AsanaClient,
    AsanaPermanentError,
    AsanaResource,
    AsanaRetryableError,
    AsanaUser,
)
from nanobot.meeting_classifier.models import (
    ApprovalSnapshot,
    PersonRef,
    ProjectDraft,
    TaskDraft,
)


@pytest.fixture
def config() -> AsanaIntegrationConfig:
    return AsanaIntegrationConfig(
        enabled=True,
        access_token="sentinel-asana-token",
        workspace_gid="workspace-1",
        team_gid="team-1",
    )


@pytest.fixture
def snapshot() -> ApprovalSnapshot:
    return ApprovalSnapshot(
        approval_id=UUID("00000000-0000-0000-0000-000000000001"),
        note_id="note-1",
        meeting_title="Weekly sync",
        meeting_date=date(2026, 7, 18),
        revision=2,
        draft=ProjectDraft(
            project="atlas",
            summary="The team agreed to ship the sync.",
        ),
    )


def _client(
    config: AsanaIntegrationConfig,
    handler,
) -> AsanaClient:
    return AsanaClient(config, transport=httpx.MockTransport(handler))


def _assert_sanitized_exception(
    error: BaseException,
    *sentinels: str,
) -> None:
    """Inspect every observable exception surface and chained exception."""
    pending = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        rendered = " ".join(
            (
                str(current),
                repr(current),
                repr(current.args),
                repr(vars(current)),
            )
        )
        for sentinel in sentinels:
            assert sentinel not in rendered
        if current.__context__ is not None:
            pending.append(current.__context__)
        if current.__cause__ is not None:
            pending.append(current.__cause__)

    assert error.__context__ is None
    assert error.__cause__ is None


async def test_validate_connection_checks_configured_workspace_and_team(config):
    seen: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        assert request.headers["Authorization"] == "Bearer sentinel-asana-token"
        return httpx.Response(200, json={"data": {"gid": "ok"}})

    client = _client(config, handler)
    try:
        await client.validate_connection()
    finally:
        await client.aclose()

    assert seen == [
        ("GET", "/api/1.0/workspaces/workspace-1"),
        ("GET", "/api/1.0/teams/team-1"),
    ]


async def test_resolve_user_by_email_paginates_and_matches_normalized_email_exactly(config):
    offsets: list[str | None] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/1.0/users"
        assert request.url.params["workspace"] == "workspace-1"
        offsets.append(request.url.params.get("offset"))
        if "offset" not in request.url.params:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "gid": "near-match",
                            "name": "Near",
                            "email": "ashleyn+other@example.edu",
                        }
                    ],
                    "next_page": {"offset": "page-2"},
                },
            )
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "gid": "user-1",
                        "name": "Ashleyn",
                        "email": "ASHLEYN@EXAMPLE.EDU",
                    }
                ],
                "next_page": None,
            },
        )

    client = _client(config, handler)
    try:
        user = await client.resolve_user_by_email(" Ashleyn@Example.edu ")
    finally:
        await client.aclose()

    assert user == AsanaUser(
        gid="user-1",
        name="Ashleyn",
        email="ashleyn@example.edu",
    )
    assert offsets == [None, "page-2"]


async def test_resolve_user_rejects_missing_and_ambiguous_exact_matches(config):
    responses = [
        {"data": [], "next_page": None},
        {
            "data": [
                {"gid": "user-1", "name": "One", "email": "person@example.edu"},
                {"gid": "user-2", "name": "Two", "email": "PERSON@example.edu"},
            ],
            "next_page": None,
        },
    ]

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=responses.pop(0))

    client = _client(config, handler)
    try:
        with pytest.raises(AsanaPermanentError) as missing:
            await client.resolve_user_by_email("missing@example.edu")
        with pytest.raises(AsanaAmbiguousError):
            await client.resolve_user_by_email("person@example.edu")
    finally:
        await client.aclose()

    assert missing.value.status_code == 404


async def test_get_project_returns_typed_resource(config):
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/1.0/projects/project-1"
        return httpx.Response(
            200,
            json={
                "data": {
                    "gid": "project-1",
                    "name": "Atlas",
                    "notes": "Research project",
                    "permalink_url": "https://app.asana.com/0/project-1/list",
                }
            },
        )

    client = _client(config, handler)
    try:
        project = await client.get_project("project-1")
    finally:
        await client.aclose()

    assert project == AsanaResource(
        gid="project-1",
        name="Atlas",
        notes="Research project",
        permalink_url="https://app.asana.com/0/project-1/list",
    )


async def test_create_project_is_private_and_includes_marker(config):
    seen: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/1.0/projects"
        seen.update(json.loads(request.content)["data"])
        return httpx.Response(
            201,
            json={
                "data": {
                    "gid": "project-1",
                    "name": "Atlas",
                    "notes": seen["notes"],
                    "permalink_url": "https://app.asana.com/0/project-1/list",
                }
            },
        )

    client = _client(config, handler)
    try:
        project = await client.create_project(
            name="Atlas",
            notes="Research project",
            marker="projectclaw:approval:a1:project",
        )
    finally:
        await client.aclose()

    assert seen == {
        "workspace": "workspace-1",
        "team": "team-1",
        "name": "Atlas",
        "notes": "Research project\n\nprojectclaw:approval:a1:project",
        "privacy_setting": "private",
    }
    assert project.gid == "project-1"


async def test_create_project_appends_only_an_exact_standalone_marker(config):
    seen: dict = {}
    requested_marker = "projectclaw:approval:a1:task:t1"

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content)["data"])
        return httpx.Response(
            201,
            json={
                "data": {
                    "gid": "project-1",
                    "name": "Atlas",
                    "notes": seen["notes"],
                }
            },
        )

    client = _client(config, handler)
    try:
        await client.create_project(
            name="Atlas",
            notes="projectclaw:approval:a1:task:t10",
            marker=requested_marker,
        )
    finally:
        await client.aclose()

    assert seen["notes"].splitlines() == [
        "projectclaw:approval:a1:task:t10",
        "",
        requested_marker,
    ]


async def test_project_owner_and_membership_use_explicit_user_gids(config):
    seen: list[tuple[str, str, dict]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, json.loads(request.content)["data"]))
        return httpx.Response(200, json={"data": {"gid": "project-1"}})

    client = _client(config, handler)
    try:
        await client.set_project_owner("project-1", "lead-1")
        await client.add_project_members("project-1", ["lead-1", "member-2"])
    finally:
        await client.aclose()

    assert seen == [
        ("PUT", "/api/1.0/projects/project-1", {"owner": "lead-1"}),
        (
            "POST",
            "/api/1.0/projects/project-1/addMembers",
            {"members": ["lead-1", "member-2"]},
        ),
    ]


async def test_find_project_paginates_exact_names_and_verifies_marker_in_fetched_notes(config):
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}?{request.url.query.decode()}")
        if request.url.path == "/api/1.0/projects":
            assert request.url.params["workspace"] == "workspace-1"
            assert request.url.params["team"] == "team-1"
            if "offset" not in request.url.params:
                return httpx.Response(
                    200,
                    json={
                        "data": [{"gid": "same-name-wrong", "name": "Atlas"}],
                        "next_page": {"offset": "page-2"},
                    },
                )
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"gid": "right", "name": "Atlas"},
                        {"gid": "different-name", "name": "Atlas II"},
                    ],
                    "next_page": None,
                },
            )
        gid = request.url.path.rsplit("/", 1)[-1]
        notes = (
            "Other marker"
            if gid == "same-name-wrong"
            else "Research\n\nprojectclaw:approval:a1:project"
        )
        return httpx.Response(
            200,
            json={"data": {"gid": gid, "name": "Atlas", "notes": notes}},
        )

    client = _client(config, handler)
    try:
        project = await client.find_project_by_marker(
            "Atlas",
            "projectclaw:approval:a1:project",
        )
    finally:
        await client.aclose()

    assert project is not None
    assert project.gid == "right"
    assert any("offset=page-2" in request for request in seen)
    assert sum("/projects/same-name-wrong" in request for request in seen) == 1
    assert sum("/projects/right" in request for request in seen) == 1
    assert not any("/projects/different-name" in request for request in seen)


@pytest.mark.parametrize("match_count", [0, 2])
async def test_find_project_returns_none_or_raises_when_marker_matches_are_not_unique(
    config,
    match_count,
):
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/1.0/projects":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"gid": f"project-{index}", "name": "Atlas"}
                        for index in range(match_count)
                    ],
                    "next_page": None,
                },
            )
        gid = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(
            200,
            json={"data": {"gid": gid, "name": "Atlas", "notes": "safe marker"}},
        )

    client = _client(config, handler)
    try:
        if match_count == 0:
            assert await client.find_project_by_marker("Atlas", "safe marker") is None
        else:
            with pytest.raises(AsanaAmbiguousError):
                await client.find_project_by_marker("Atlas", "safe marker")
    finally:
        await client.aclose()


async def test_find_project_does_not_match_a_longer_marker_line(config):
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/1.0/projects":
            return httpx.Response(
                200,
                json={
                    "data": [{"gid": "project-1", "name": "Atlas"}],
                    "next_page": None,
                },
            )
        return httpx.Response(
            200,
            json={
                "data": {
                    "gid": "project-1",
                    "name": "Atlas",
                    "notes": "projectclaw:approval:a1:task:t10",
                }
            },
        )

    client = _client(config, handler)
    try:
        project = await client.find_project_by_marker(
            "Atlas",
            "projectclaw:approval:a1:task:t1",
        )
    finally:
        await client.aclose()

    assert project is None


async def test_create_parent_task_is_unassigned_and_records_snapshot_provenance(
    config,
    snapshot,
):
    seen: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/1.0/tasks"
        seen.update(json.loads(request.content)["data"])
        return httpx.Response(
            201,
            json={
                "data": {
                    "gid": "parent-1",
                    "name": seen["name"],
                    "notes": seen["notes"],
                    "permalink_url": "https://app.asana.com/0/1/parent-1",
                }
            },
        )

    client = _client(config, handler)
    try:
        parent = await client.create_parent_task(
            "project-1",
            snapshot,
            "projectclaw:approval:a1:parent",
        )
    finally:
        await client.aclose()

    assert seen["workspace"] == "workspace-1"
    assert seen["projects"] == ["project-1"]
    assert seen["name"] == "Weekly sync — 2026-07-18"
    assert "The team agreed to ship the sync." in seen["notes"]
    assert "projectclaw:approval:a1:parent" in seen["notes"]
    assert "assignee" not in seen
    assert "due_on" not in seen
    assert parent.permalink_url == "https://app.asana.com/0/1/parent-1"


async def test_create_subtask_uses_one_assignee_and_explicit_due_date(config):
    seen: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/1.0/tasks/parent-1/subtasks"
        seen.update(json.loads(request.content)["data"])
        return httpx.Response(
            201,
            json={
                "data": {
                    "gid": "task-1",
                    "name": seen["name"],
                    "notes": seen["notes"],
                    "permalink_url": "https://app.asana.com/0/1/task-1",
                }
            },
        )

    task = TaskDraft(
        id="t1",
        title="Ship sync",
        owner=PersonRef(name="Ashleyn", email="ashleyn@example.edu"),
        due_on=date(2026, 7, 24),
        due_on_source="meeting",
    )
    client = _client(config, handler)
    try:
        await client.create_subtask(
            parent_gid="parent-1",
            task=task,
            assignee_gid="user-1",
            marker="projectclaw:approval:a1:task:t1",
        )
    finally:
        await client.aclose()

    assert seen["name"] == "Ship sync"
    assert seen["assignee"] == "user-1"
    assert seen["due_on"] == "2026-07-24"
    assert "projectclaw:approval:a1:task:t1" in seen["notes"]


async def test_create_subtask_does_not_invent_assignee_or_due_date(config):
    seen: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content)["data"])
        return httpx.Response(
            201,
            json={"data": {"gid": "task-1", "name": "Unassigned", "notes": "marker"}},
        )

    client = _client(config, handler)
    try:
        await client.create_subtask(
            "parent-1",
            TaskDraft(id="t1", title="Unassigned"),
            None,
            "marker",
        )
    finally:
        await client.aclose()

    assert "assignee" not in seen
    assert "due_on" not in seen


async def test_find_task_paginates_subtasks_and_verifies_marker_in_fetched_notes(config):
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path == "/api/1.0/tasks/parent-1/subtasks":
            if "offset" not in request.url.params:
                return httpx.Response(
                    200,
                    json={
                        "data": [{"gid": "wrong", "name": "Task"}],
                        "next_page": {"offset": "next"},
                    },
                )
            return httpx.Response(
                200,
                json={
                    "data": [{"gid": "right", "name": "Task"}],
                    "next_page": None,
                },
            )
        gid = request.url.path.rsplit("/", 1)[-1]
        notes = "projectclaw:approval:a1:task:t1" if gid == "right" else "other"
        return httpx.Response(
            200,
            json={"data": {"gid": gid, "name": "Task", "notes": notes}},
        )

    client = _client(config, handler)
    try:
        task = await client.find_task_by_marker(
            "parent-1",
            "projectclaw:approval:a1:task:t1",
        )
    finally:
        await client.aclose()

    assert task is not None
    assert task.gid == "right"
    assert any("offset=next" in call for call in calls)


async def test_find_task_rejects_ambiguous_markers(config):
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/subtasks"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"gid": "task-1", "name": "One"},
                        {"gid": "task-2", "name": "Two"},
                    ],
                    "next_page": None,
                },
            )
        gid = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(
            200,
            json={"data": {"gid": gid, "name": gid, "notes": "same marker"}},
        )

    client = _client(config, handler)
    try:
        with pytest.raises(AsanaAmbiguousError):
            await client.find_task_by_marker("parent-1", "same marker")
    finally:
        await client.aclose()


async def test_find_task_does_not_match_a_longer_marker_line(config):
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/subtasks"):
            return httpx.Response(
                200,
                json={
                    "data": [{"gid": "task-10", "name": "Ten"}],
                    "next_page": None,
                },
            )
        return httpx.Response(
            200,
            json={
                "data": {
                    "gid": "task-10",
                    "name": "Ten",
                    "notes": "projectclaw:approval:a1:task:t10",
                }
            },
        )

    client = _client(config, handler)
    try:
        task = await client.find_task_by_marker(
            "parent-1",
            "projectclaw:approval:a1:task:t1",
        )
    finally:
        await client.aclose()

    assert task is None


async def test_find_parent_task_paginates_project_tasks_and_requires_exact_marker(config):
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path == "/api/1.0/tasks":
            assert request.url.params["project"] == "project-1"
            if "offset" not in request.url.params:
                return httpx.Response(
                    200,
                    json={
                        "data": [{"gid": "parent-10", "name": "Weekly sync"}],
                        "next_page": {"offset": "next"},
                    },
                )
            return httpx.Response(
                200,
                json={
                    "data": [{"gid": "parent-1", "name": "Weekly sync"}],
                    "next_page": None,
                },
            )
        gid = request.url.path.rsplit("/", 1)[-1]
        marker = (
            "projectclaw:approval:a1:parent"
            if gid == "parent-1"
            else "projectclaw:approval:a1:parent:old"
        )
        return httpx.Response(
            200,
            json={"data": {"gid": gid, "name": "Weekly sync", "notes": marker}},
        )

    client = _client(config, handler)
    try:
        parent = await client.find_parent_task_by_marker(
            "project-1",
            "projectclaw:approval:a1:parent",
        )
    finally:
        await client.aclose()

    assert parent is not None
    assert parent.gid == "parent-1"
    assert any("offset=next" in call for call in calls)


@pytest.mark.parametrize("match_count", [0, 2])
async def test_find_parent_task_returns_none_or_raises_for_non_unique_markers(
    config,
    match_count,
):
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/1.0/tasks":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"gid": f"parent-{index}", "name": "Weekly sync"}
                        for index in range(match_count)
                    ],
                    "next_page": None,
                },
            )
        gid = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(
            200,
            json={"data": {"gid": gid, "name": "Weekly sync", "notes": "marker"}},
        )

    client = _client(config, handler)
    try:
        if match_count == 0:
            assert await client.find_parent_task_by_marker("project-1", "marker") is None
        else:
            with pytest.raises(AsanaAmbiguousError):
                await client.find_parent_task_by_marker("project-1", "marker")
    finally:
        await client.aclose()


async def test_add_task_followers_uses_only_explicit_user_gids(config):
    seen: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/1.0/tasks/task-1/addFollowers"
        seen.update(json.loads(request.content)["data"])
        return httpx.Response(200, json={"data": {"gid": "task-1"}})

    client = _client(config, handler)
    try:
        await client.add_task_followers("task-1", ["user-2", "user-3"])
    finally:
        await client.aclose()

    assert seen == {"followers": ["user-2", "user-3"]}


@pytest.mark.parametrize(
    "retry_after,expected",
    [("12", 12.0), ("1.5", 1.5), ("not-a-number", None), ("-3", None)],
)
async def test_rate_limit_is_retryable_and_retry_after_is_parsed_defensively(
    config,
    retry_after,
    expected,
):
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"Retry-After": retry_after},
            json={"errors": [{"message": "sentinel-remote-error"}]},
        )

    client = _client(config, handler)
    try:
        with pytest.raises(AsanaRetryableError) as caught:
            await client.validate_connection()
    finally:
        await client.aclose()

    assert caught.value.retry_after == expected
    assert caught.value.status_code == 429
    assert "sentinel-remote-error" not in str(caught.value)


async def test_unauthorized_error_never_retains_headers_body_token_dsn_or_remote_text(config):
    body_secret = "sentinel-body-secret"
    dsn_secret = "postgresql://sentinel-dsn-secret@example.invalid/database"

    async def handler(request: httpx.Request) -> httpx.Response:
        assert "sentinel-asana-token" in request.headers["Authorization"]
        return httpx.Response(
            401,
            headers={"X-Sentinel-Secret": "sentinel-header-secret"},
            json={
                "errors": [
                    {
                        "message": (
                            f"remote said {body_secret} with {dsn_secret} and "
                            "sentinel-asana-token"
                        )
                    }
                ]
            },
        )

    client = _client(config, handler)
    try:
        with pytest.raises(AsanaPermanentError) as caught:
            await client.get_project("project-1")
    finally:
        await client.aclose()

    error = caught.value
    assert error.status_code == 401
    assert error.safe_message == "Asana request failed."
    _assert_sanitized_exception(
        error,
        "sentinel-asana-token",
        "sentinel-header-secret",
        body_secret,
        dsn_secret,
        "remote said",
    )


async def test_malformed_json_error_has_no_raw_exception_context(config):
    body_secret = "sentinel-malformed-json-body"

    async def handler(request: httpx.Request) -> httpx.Response:
        assert "sentinel-asana-token" in request.headers["Authorization"]
        return httpx.Response(200, content=f'{{"data": "{body_secret}"')

    client = _client(config, handler)
    try:
        with pytest.raises(AsanaPermanentError) as caught:
            await client.get_project("project-1")
    finally:
        await client.aclose()

    assert caught.value.status_code == 200
    _assert_sanitized_exception(
        caught.value,
        body_secret,
        "sentinel-asana-token",
    )


@pytest.mark.parametrize(
    "status_code,error_type",
    [
        (400, AsanaPermanentError),
        (403, AsanaPermanentError),
        (404, AsanaPermanentError),
        (409, AsanaPermanentError),
        (500, AsanaRetryableError),
        (503, AsanaRetryableError),
        (600, AsanaPermanentError),
    ],
)
async def test_non_success_statuses_have_safe_deterministic_classification(
    config,
    status_code,
    error_type,
):
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, text="sentinel unsafe remote response")

    client = _client(config, handler)
    try:
        with pytest.raises(error_type) as caught:
            await client.get_project("project-1")
    finally:
        await client.aclose()

    assert caught.value.status_code == status_code
    assert "sentinel" not in str(caught.value)


async def test_timeout_is_retryable_and_next_attempt_can_reconcile_created_project(config):
    attempts = 0
    marker = "projectclaw:approval:a1:project"

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if request.method == "POST":
            attempts += 1
            raise httpx.ReadTimeout(
                "sentinel timeout included token sentinel-asana-token",
                request=request,
            )
        if request.url.path == "/api/1.0/projects":
            return httpx.Response(
                200,
                json={
                    "data": [{"gid": "created-remotely", "name": "Atlas"}],
                    "next_page": None,
                },
            )
        return httpx.Response(
            200,
            json={
                "data": {
                    "gid": "created-remotely",
                    "name": "Atlas",
                    "notes": f"Research\n\n{marker}",
                    "permalink_url": "https://app.asana.com/0/created-remotely/list",
                }
            },
        )

    client = _client(config, handler)
    try:
        with pytest.raises(AsanaRetryableError) as caught:
            await client.create_project(name="Atlas", notes="Research", marker=marker)
        reconciled = await client.find_project_by_marker("Atlas", marker)
    finally:
        await client.aclose()

    _assert_sanitized_exception(caught.value, "sentinel", "sentinel-asana-token")
    assert attempts == 1
    assert reconciled is not None
    assert reconciled.gid == "created-remotely"


async def test_timeout_after_parent_create_reconciles_before_duplicate_retry(config, snapshot):
    marker = "projectclaw:approval:a1:parent"
    posts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        if request.method == "POST":
            posts += 1
            raise httpx.ReadTimeout(
                "sentinel parent timeout sentinel-asana-token",
                request=request,
            )
        if request.url.path == "/api/1.0/tasks":
            assert request.url.params["project"] == "project-1"
            return httpx.Response(
                200,
                json={
                    "data": [{"gid": "parent-created-remotely", "name": "Weekly sync"}],
                    "next_page": None,
                },
            )
        return httpx.Response(
            200,
            json={
                "data": {
                    "gid": "parent-created-remotely",
                    "name": "Weekly sync — 2026-07-18",
                    "notes": f"Summary\n\n{marker}",
                }
            },
        )

    client = _client(config, handler)
    try:
        with pytest.raises(AsanaRetryableError) as caught:
            await client.create_parent_task("project-1", snapshot, marker)
        reconciled = await client.find_parent_task_by_marker("project-1", marker)
    finally:
        await client.aclose()

    _assert_sanitized_exception(caught.value, "sentinel", "sentinel-asana-token")
    assert posts == 1
    assert reconciled is not None
    assert reconciled.gid == "parent-created-remotely"


@pytest.mark.parametrize(
    "operation,status_code,response",
    [
        ("read", 200, {"unexpected": {}}),
        ("create", 201, {"data": []}),
        ("void", 200, {"data": None}),
        ("void_empty", 204, None),
    ],
)
async def test_malformed_success_envelopes_are_sanitized_with_actual_status(
    config,
    operation,
    status_code,
    response,
):
    async def handler(_request: httpx.Request) -> httpx.Response:
        if response is None:
            return httpx.Response(status_code, content=b"")
        return httpx.Response(status_code, json=response)

    client = _client(config, handler)
    try:
        with pytest.raises(AsanaPermanentError) as caught:
            if operation == "read":
                await client.get_project("project-1")
            elif operation == "create":
                await client.create_project(name="Atlas", notes="Research", marker="marker")
            elif operation == "void":
                await client.set_project_owner("project-1", "user-1")
            else:
                await client.add_task_followers("task-1", ["user-1"])
    finally:
        await client.aclose()

    assert caught.value.status_code == status_code
    _assert_sanitized_exception(caught.value, "sentinel-asana-token")


async def test_aclose_closes_the_reusable_http_client(config):
    requests = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json={"data": {"gid": "ok"}})

    client = _client(config, handler)
    await client.validate_connection()
    await client.validate_connection()
    await client.aclose()

    assert requests == 4
    with pytest.raises(RuntimeError, match="closed"):
        await client.validate_connection()
