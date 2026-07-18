"""Small, sanitized HTTP boundary for the approved Asana workflow."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import httpx

from nanobot.config.schema import AsanaIntegrationConfig
from nanobot.integrations.errors import safe_external_error
from nanobot.meeting_classifier.models import ApprovalSnapshot, TaskDraft

_SAFE_REQUEST_ERROR = safe_external_error("Asana", "request")
_SAFE_NOT_FOUND = "Asana resource was not found."
_SAFE_AMBIGUOUS = "Asana reconciliation was ambiguous."
_RESOURCE_FIELDS = "gid,name,notes,permalink_url"


class _ResponseDict(dict):
    """A private dict-compatible response value retaining only safe status metadata."""

    def __init__(self, value: dict, status_code: int) -> None:
        super().__init__(value)
        self.status_code = status_code


@dataclass(frozen=True)
class AsanaUser:
    """The identity fields used to join an Asana workspace user by email."""

    gid: str
    name: str
    email: str


@dataclass(frozen=True)
class AsanaResource:
    """The common fields provisioning needs from an Asana project or task."""

    gid: str
    name: str
    notes: str = ""
    permalink_url: str | None = None


class AsanaRetryableError(Exception):
    """A sanitized failure that may succeed on a later attempt."""

    def __init__(
        self,
        safe_message: str,
        retry_after: float | None = None,
        *,
        status_code: int | None = None,
        operation: str = "request",
    ) -> None:
        super().__init__(safe_message)
        self.safe_message = safe_message
        self.retry_after = retry_after
        self.status_code = status_code
        self.operation = operation

    def __str__(self) -> str:
        metadata = [f"operation={self.operation}"]
        if self.status_code is not None:
            metadata.append(f"status={self.status_code}")
        if self.retry_after is not None:
            metadata.append(f"retry_after={self.retry_after:g}")
        return f"{self.safe_message} ({', '.join(metadata)})"


class AsanaPermanentError(Exception):
    """A sanitized failure that requires correction or operator attention."""

    def __init__(
        self,
        status_code: int,
        safe_message: str,
        *,
        operation: str = "request",
    ) -> None:
        super().__init__(safe_message)
        self.status_code = status_code
        self.safe_message = safe_message
        self.operation = operation

    def __str__(self) -> str:
        return (
            f"{self.safe_message} "
            f"(operation={self.operation}, status={self.status_code})"
        )


class AsanaAmbiguousError(Exception):
    """More than one remote resource carries the expected provenance marker."""

    def __init__(self, *, operation: str = "reconcile") -> None:
        super().__init__(_SAFE_AMBIGUOUS)
        self.safe_message = _SAFE_AMBIGUOUS
        self.operation = operation

    def __str__(self) -> str:
        return f"{self.safe_message} (operation={self.operation})"


class AsanaClient:
    """Typed operations for the single configured Asana workspace and team."""

    def __init__(
        self,
        config: AsanaIntegrationConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._config = config
        self._client = httpx.AsyncClient(
            base_url=f"{config.base_url.rstrip('/')}/",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {config.access_token}",
            },
            timeout=10.0,
            transport=transport,
        )

    async def aclose(self) -> None:
        """Close the one reusable HTTP client owned by this adapter."""
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        operation = method.upper()
        response: httpx.Response | None = None
        transport_failed = False
        try:
            response = await self._client.request(
                operation,
                path.lstrip("/"),
                json=json,
                params=params,
            )
        except httpx.TransportError:
            transport_failed = True

        if transport_failed:
            raise AsanaRetryableError(
                _SAFE_REQUEST_ERROR,
                operation=operation,
            )
        if response is None:  # pragma: no cover - defensive type narrowing
            raise AsanaRetryableError(_SAFE_REQUEST_ERROR, operation=operation)

        status_code = response.status_code
        if status_code == 429 or 500 <= status_code < 600:
            raise AsanaRetryableError(
                _SAFE_REQUEST_ERROR,
                _retry_after(response.headers.get("Retry-After")),
                status_code=status_code,
                operation=operation,
            )
        if not 200 <= status_code < 300:
            raise AsanaPermanentError(
                status_code,
                _SAFE_REQUEST_ERROR,
                operation=operation,
            )

        payload: Any = None
        decoding_failed = False
        try:
            payload = response.json()
        except ValueError:
            decoding_failed = True

        if decoding_failed:
            raise AsanaPermanentError(
                status_code,
                _SAFE_REQUEST_ERROR,
                operation=operation,
            )
        if not isinstance(payload, dict) or "data" not in payload:
            raise AsanaPermanentError(
                status_code,
                _SAFE_REQUEST_ERROR,
                operation=operation,
            )
        return _ResponseDict(payload, status_code)

    async def _paginate(self, path: str, *, params: dict[str, Any]) -> list[dict]:
        query = dict(params)
        query["limit"] = 100
        resources: list[dict] = []
        seen_offsets: set[str] = set()

        while True:
            payload = await self._request("GET", path, params=query)
            page = payload.get("data")
            if not isinstance(page, list) or not all(isinstance(item, dict) for item in page):
                raise AsanaPermanentError(
                    _response_status(payload),
                    _SAFE_REQUEST_ERROR,
                    operation="GET",
                )
            resources.extend(
                _ResponseDict(item, _response_status(payload))
                for item in page
            )

            next_page = payload.get("next_page")
            if next_page is None:
                return resources
            if not isinstance(next_page, dict):
                raise AsanaPermanentError(
                    _response_status(payload),
                    _SAFE_REQUEST_ERROR,
                    operation="GET",
                )
            offset = next_page.get("offset")
            if not isinstance(offset, str) or not offset or offset in seen_offsets:
                raise AsanaPermanentError(
                    _response_status(payload),
                    _SAFE_REQUEST_ERROR,
                    operation="GET",
                )
            seen_offsets.add(offset)
            query["offset"] = offset

    async def validate_connection(self) -> None:
        """Validate access to the one configured workspace and team."""
        workspace = await self._request("GET", f"workspaces/{self._config.workspace_gid}")
        _data_object(workspace)
        team = await self._request("GET", f"teams/{self._config.team_gid}")
        _data_object(team)

    async def resolve_user_by_email(self, email: str) -> AsanaUser:
        """Resolve exactly one workspace user by normalized exact email."""
        normalized_email = email.strip().lower()
        users = await self._paginate(
            "users",
            params={
                "workspace": self._config.workspace_gid,
                "opt_fields": "gid,name,email",
            },
        )
        matches: list[AsanaUser] = []
        for user in users:
            remote_email = user.get("email")
            if not isinstance(remote_email, str):
                continue
            if remote_email.strip().lower() != normalized_email:
                continue
            matches.append(_as_user(user))

        if not matches:
            raise AsanaPermanentError(404, _SAFE_NOT_FOUND, operation="resolve_user")
        if len(matches) > 1:
            raise AsanaAmbiguousError(operation="resolve_user")
        return matches[0]

    async def get_project(self, project_gid: str) -> AsanaResource:
        """Fetch an existing mapped project and the fields provisioning consumes."""
        payload = await self._request(
            "GET",
            f"projects/{project_gid}",
            params={"opt_fields": _RESOURCE_FIELDS},
        )
        return _as_resource(_data_object(payload))

    async def find_project_by_marker(
        self,
        name: str,
        marker: str,
    ) -> AsanaResource | None:
        """Reconcile an exact-name project only when its fetched notes carry the marker."""
        _require_marker(marker)
        projects = await self._paginate(
            "projects",
            params={
                "workspace": self._config.workspace_gid,
                "team": self._config.team_gid,
                "archived": "false",
                "opt_fields": "gid,name",
            },
        )
        candidate_gids: list[str] = []
        for project in projects:
            if project.get("name") != name:
                continue
            gid = project.get("gid")
            if isinstance(gid, str) and gid not in candidate_gids:
                candidate_gids.append(gid)

        matches: list[AsanaResource] = []
        for gid in candidate_gids:
            candidate = await self.get_project(gid)
            if _has_marker(candidate.notes, marker):
                matches.append(candidate)
        return _one_or_none(matches, operation="reconcile_project")

    async def create_project(
        self,
        *,
        name: str,
        notes: str,
        marker: str,
    ) -> AsanaResource:
        """Create a private project in the configured workspace and team."""
        payload = await self._request(
            "POST",
            "projects",
            json={
                "data": {
                    "workspace": self._config.workspace_gid,
                    "team": self._config.team_gid,
                    "name": name,
                    "notes": _notes_with_marker(notes, marker),
                    "privacy_setting": "private",
                }
            },
            params={"opt_fields": _RESOURCE_FIELDS},
        )
        return _as_resource(_data_object(payload))

    async def set_project_owner(self, project_gid: str, user_gid: str) -> None:
        """Assign the explicitly approved lead as project owner."""
        payload = await self._request(
            "PUT",
            f"projects/{project_gid}",
            json={"data": {"owner": user_gid}},
        )
        _data_object(payload)

    async def add_project_members(self, project_gid: str, user_gids: list[str]) -> None:
        """Add only the explicitly supplied workspace users to a project."""
        if not user_gids:
            return
        payload = await self._request(
            "POST",
            f"projects/{project_gid}/addMembers",
            json={"data": {"members": list(user_gids)}},
        )
        _data_object(payload)

    async def find_parent_task_by_marker(
        self,
        project_gid: str,
        marker: str,
    ) -> AsanaResource | None:
        """Reconcile a parent task within its known project by an exact notes marker."""
        _require_marker(marker)
        tasks = await self._paginate(
            "tasks",
            params={
                "project": project_gid,
                "opt_fields": "gid,name",
            },
        )
        return await self._reconcile_tasks(
            tasks,
            marker,
            operation="reconcile_parent_task",
        )

    async def find_task_by_marker(
        self,
        parent_gid: str,
        marker: str,
    ) -> AsanaResource | None:
        """Reconcile a subtask beneath its known parent by a notes marker."""
        _require_marker(marker)
        tasks = await self._paginate(
            f"tasks/{parent_gid}/subtasks",
            params={"opt_fields": "gid,name"},
        )
        return await self._reconcile_tasks(
            tasks,
            marker,
            operation="reconcile_subtask",
        )

    async def _reconcile_tasks(
        self,
        tasks: list[dict],
        marker: str,
        *,
        operation: str,
    ) -> AsanaResource | None:
        candidate_gids: list[str] = []
        for task in tasks:
            gid = task.get("gid")
            if isinstance(gid, str) and gid not in candidate_gids:
                candidate_gids.append(gid)

        matches: list[AsanaResource] = []
        for gid in candidate_gids:
            candidate = await self._get_task(gid)
            if _has_marker(candidate.notes, marker):
                matches.append(candidate)
        return _one_or_none(matches, operation=operation)

    async def create_parent_task(
        self,
        project_gid: str,
        snapshot: ApprovalSnapshot,
        marker: str,
    ) -> AsanaResource:
        """Create the unassigned meeting parent task in an existing project."""
        payload = await self._request(
            "POST",
            "tasks",
            json={
                "data": {
                    "workspace": self._config.workspace_gid,
                    "projects": [project_gid],
                    "name": (
                        f"{snapshot.meeting_title} — "
                        f"{snapshot.meeting_date.isoformat()}"
                    ),
                    "notes": _notes_with_marker(snapshot.draft.summary, marker),
                }
            },
            params={"opt_fields": _RESOURCE_FIELDS},
        )
        return _as_resource(_data_object(payload))

    async def create_subtask(
        self,
        parent_gid: str,
        task: TaskDraft,
        assignee_gid: str | None,
        marker: str,
    ) -> AsanaResource:
        """Create one approved action item without inventing assignment or a due date."""
        data: dict[str, Any] = {
            "name": task.title,
            "notes": _notes_with_marker("", marker),
        }
        if assignee_gid is not None:
            data["assignee"] = assignee_gid
        if task.due_on is not None:
            data["due_on"] = task.due_on.isoformat()

        payload = await self._request(
            "POST",
            f"tasks/{parent_gid}/subtasks",
            json={"data": data},
            params={"opt_fields": _RESOURCE_FIELDS},
        )
        return _as_resource(_data_object(payload))

    async def add_task_followers(self, task_gid: str, user_gids: list[str]) -> None:
        """Add only explicitly approved collaborators as task followers."""
        if not user_gids:
            return
        payload = await self._request(
            "POST",
            f"tasks/{task_gid}/addFollowers",
            json={"data": {"followers": list(user_gids)}},
        )
        _data_object(payload)

    async def _get_task(self, task_gid: str) -> AsanaResource:
        payload = await self._request(
            "GET",
            f"tasks/{task_gid}",
            params={"opt_fields": _RESOURCE_FIELDS},
        )
        return _as_resource(_data_object(payload))


def _retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        retry_after = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(retry_after) or retry_after < 0:
        return None
    return retry_after


def _require_marker(marker: str) -> None:
    if not marker or marker.strip() != marker or len(marker.splitlines()) != 1:
        raise ValueError("Asana provenance marker must not be blank.")


def _has_marker(notes: str, marker: str) -> bool:
    _require_marker(marker)
    return marker in notes.splitlines()


def _notes_with_marker(notes: str, marker: str) -> str:
    _require_marker(marker)
    if _has_marker(notes, marker):
        return notes
    visible_notes = notes.rstrip()
    if not visible_notes:
        return marker
    return f"{visible_notes}\n\n{marker}"


def _response_status(value: dict) -> int:
    status_code = getattr(value, "status_code", 200)
    return status_code if isinstance(status_code, int) else 200


def _data_object(payload: dict) -> dict:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise AsanaPermanentError(
            _response_status(payload),
            _SAFE_REQUEST_ERROR,
            operation="parse",
        )
    return _ResponseDict(data, _response_status(payload))


def _as_user(data: dict) -> AsanaUser:
    gid = data.get("gid")
    name = data.get("name")
    email = data.get("email")
    if not isinstance(gid, str) or not isinstance(name, str) or not isinstance(email, str):
        raise AsanaPermanentError(
            _response_status(data),
            _SAFE_REQUEST_ERROR,
            operation="parse",
        )
    return AsanaUser(gid=gid, name=name, email=email.strip().lower())


def _as_resource(data: dict) -> AsanaResource:
    gid = data.get("gid")
    name = data.get("name", "")
    notes = data.get("notes", "")
    permalink_url = data.get("permalink_url")
    if (
        not isinstance(gid, str)
        or not isinstance(name, str)
        or not isinstance(notes, str)
        or (permalink_url is not None and not isinstance(permalink_url, str))
    ):
        raise AsanaPermanentError(
            _response_status(data),
            _SAFE_REQUEST_ERROR,
            operation="parse",
        )
    return AsanaResource(
        gid=gid,
        name=name,
        notes=notes,
        permalink_url=permalink_url,
    )


def _one_or_none(
    resources: list[AsanaResource],
    *,
    operation: str,
) -> AsanaResource | None:
    if not resources:
        return None
    if len(resources) > 1:
        raise AsanaAmbiguousError(operation=operation)
    return resources[0]
