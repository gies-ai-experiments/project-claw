"""MindForum programmatic API tools: create room, send invites.

Thin wrapper over the MindForum v1 REST API (Bearer auth).
See https://github.com/gies-ai-experiments/MindForum/blob/test/docs/programmatic-api.md

Tools deliberately do NOT raise on HTTP errors. They return a short
structured error string instead, so an agent can surface "couldn't reach
MindForum" inline rather than aborting the turn. The api_key is treated as a
secret: it is never included in error strings or tool results (httpx request
errors expose only the URL/message, not headers).
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import (
    ArraySchema,
    ObjectSchema,
    StringSchema,
    tool_parameters_schema,
)
from nanobot.config.schema import Base

_DEFAULT_TIMEOUT_S = 30
_SLUG_RE = re.compile(r"^[a-z0-9-]{3,40}$")
_MAX_INVITES_PER_REQUEST = 50


class MindForumToolConfig(Base):
    """MindForum programmatic-API tool configuration.

    Inert until ``host`` and ``api_key`` are both set (see :attr:`active`),
    mirroring the MemoryConfig.active gate pattern.
    """

    enabled: bool = False
    host: str | None = None
    api_key: str | None = None
    timeout: int = _DEFAULT_TIMEOUT_S

    @property
    def active(self) -> bool:
        return self.enabled and bool(self.host) and bool(self.api_key)


def _format_mindforum_error(resp: httpx.Response) -> str:
    code = ""
    try:
        parsed = resp.json()
        if isinstance(parsed, dict):
            code = str(parsed.get("error", ""))
    except ValueError:
        pass
    body = code or resp.text or ""
    if len(body) > 400:
        body = body[:400] + "…"
    return f"MindForum API error: HTTP {resp.status_code} — {body}"


async def _mindforum_post(
    cfg: MindForumToolConfig,
    path: str,
    body: dict[str, Any],
) -> dict[str, Any] | str:
    """POST against the MindForum API. Returns parsed JSON on 2xx, error string otherwise."""
    if not cfg.active:
        return "MindForum API error: not configured (host and api_key required)"
    url = f"{cfg.host.rstrip('/')}{path}"
    headers = {
        "Authorization": f"Bearer {cfg.api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=cfg.timeout) as client:
            resp = await client.post(url, json=body, headers=headers)
    except httpx.RequestError as exc:
        return f"MindForum API error: request failed — {exc.__class__.__name__}: {exc}"
    if resp.status_code == 429:
        retry = resp.headers.get("Retry-After", "?")
        return f"MindForum API error: rate limited (HTTP 429, retry-after={retry}s)"
    if resp.status_code >= 400:
        return _format_mindforum_error(resp)
    try:
        return resp.json()
    except ValueError:
        return f"MindForum API error: response was not JSON (HTTP {resp.status_code})"


# -------- create room --------


@tool_parameters(
    tool_parameters_schema(
        name=StringSchema(
            "Display name of the MindForum room to create.",
            min_length=1,
        ),
        system_prompt=StringSchema(
            "Optional system prompt that defines the room's persona/behavior. "
            "Rejected if it exceeds the server's MAX_SYSTEM_PROMPT_CHARS cap.",
        ),
        slug=StringSchema(
            "Optional room id slug. Must match ^[a-z0-9-]{3,40}$ (lowercase letters, "
            "digits, hyphens; 3-40 chars). Omit to let the server auto-generate one.",
        ),
        required=["name"],
    )
)
class CreateMindForumRoomTool(Tool):
    """Create a MindForum room (optionally with a system prompt and id slug)."""

    _scopes = {"core", "subagent"}
    name = "create_mindforum_room"
    description = (
        "Create a MindForum room with a display name, and optionally a system prompt and "
        "custom id slug. Returns the room id and name. Requires tools.mindforum to be "
        "configured (host + api_key)."
    )
    config_key = "mindforum"

    @classmethod
    def config_cls(cls) -> type[MindForumToolConfig]:
        return MindForumToolConfig

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        return ctx.config.mindforum.active

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(config=ctx.config.mindforum)

    def __init__(self, config: MindForumToolConfig | None = None) -> None:
        self.config = config if config is not None else MindForumToolConfig()

    async def execute(self, **kwargs: Any) -> str:
        room_name = kwargs.get("name")
        if not room_name:
            return "MindForum API error: name is required"
        slug = kwargs.get("slug")
        if slug and not _SLUG_RE.match(slug):
            return (
                f'Error: invalid slug "{slug}" — must match ^[a-z0-9-]{3,40}$ '
                "(lowercase letters, digits, hyphens; 3-40 chars)."
            )
        body: dict[str, Any] = {"name": room_name}
        if slug:
            body["id"] = slug
        system_prompt = kwargs.get("system_prompt")
        if system_prompt:
            body["systemPrompt"] = system_prompt
        result = await _mindforum_post(self.config, "/api/v1/rooms", body)
        if isinstance(result, str):
            return result
        room_id = result.get("id", "")
        returned_name = result.get("name", room_name)
        return f'Created MindForum room "{returned_name}" (id: {room_id}).'


# -------- invite to room --------


@tool_parameters(
    tool_parameters_schema(
        room_id=StringSchema(
            "id slug of the MindForum room to invite to. You must own the room "
            "(else the API returns not_found).",
            min_length=1,
        ),
        invites=ArraySchema(
            ObjectSchema(
                properties={
                    "invitee_email": StringSchema("Invitee email address."),
                    "invitee_name": StringSchema("Invitee display name."),
                },
                required=["invitee_email", "invitee_name"],
            ),
            description=(
                "Invitees to email. Max 50 per call (server limit); "
                "already-invited duplicates are counted as skipped, not errors."
            ),
            min_items=1,
            max_items=_MAX_INVITES_PER_REQUEST,
        ),
        required=["room_id", "invites"],
    )
)
class InviteToMindForumRoomTool(Tool):
    """Send MindForum room invitations (bulk, up to 50 per call)."""

    _scopes = {"core", "subagent"}
    name = "invite_to_mindforum_room"
    description = (
        "Send invitations to a MindForum room you own. Accepts a list of "
        "{invitee_email, invitee_name} objects (max 50). Already-invited duplicates "
        "are reported as skipped, not failures. Requires tools.mindforum to be configured."
    )
    config_key = "mindforum"

    @classmethod
    def config_cls(cls) -> type[MindForumToolConfig]:
        return MindForumToolConfig

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        return ctx.config.mindforum.active

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(config=ctx.config.mindforum)

    def __init__(self, config: MindForumToolConfig | None = None) -> None:
        self.config = config if config is not None else MindForumToolConfig()

    async def execute(self, **kwargs: Any) -> str:
        room_id = kwargs.get("room_id")
        if not room_id:
            return "MindForum API error: room_id is required"
        invites_in = kwargs.get("invites")
        if not invites_in:
            return "MindForum API error: invites is required (min 1)"
        if len(invites_in) > _MAX_INVITES_PER_REQUEST:
            return (
                f"Error: too many invites ({len(invites_in)}) — max "
                f"{_MAX_INVITES_PER_REQUEST} per call."
            )
        payload = [
            {
                "inviteeEmail": inv.get("invitee_email", ""),
                "inviteeName": inv.get("invitee_name", ""),
            }
            for inv in invites_in
        ]
        result = await _mindforum_post(
            self.config, f"/api/v1/rooms/{room_id}/invitations", {"invites": payload}
        )
        if isinstance(result, str):
            return result
        created = result.get("created", [])
        skipped = result.get("skipped", 0)
        return (
            f'Invited {len(created)} to "{room_id}"'
            + (f" (skipped: {skipped} already-invited)" if skipped else "")
            + "."
        )


__all__ = [
    "MindForumToolConfig",
    "CreateMindForumRoomTool",
    "InviteToMindForumRoomTool",
]
