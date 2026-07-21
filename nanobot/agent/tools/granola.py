"""Granola public API tools: list notes, get note, list folders.

Thin wrapper over https://public-api.granola.ai/v1 (REST, Bearer auth).
See https://docs.granola.ai/introduction.

Tools deliberately do NOT raise on HTTP errors. They return a short
structured error string instead, so the projectclaw skill's
"partial-answer on tool failure" rule keeps working — an agent can
surface "couldn't reach Granola" inline rather than aborting the turn.
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timezone
from typing import Any

import httpx

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import (
    BooleanSchema,
    IntegerSchema,
    StringSchema,
    tool_parameters_schema,
)
from nanobot.config.schema import Base

_DEFAULT_BASE_URL = "https://public-api.granola.ai/v1"
_DEFAULT_TIMEOUT_S = 30


class GranolaToolConfig(Base):
    """Granola public-API tool configuration."""

    enable: bool = True
    api_key: str = ""
    base_url: str = _DEFAULT_BASE_URL
    timeout: int = _DEFAULT_TIMEOUT_S
    # Test-only: when > 0, _granola_get returns this many generated gibberish
    # meetings instead of calling the real API, so the meeting pipeline can be
    # driven without real Granola notes. Off (0) in production.
    fake_meetings: int = 0


def _format_http_error(resp: httpx.Response) -> str:
    body = resp.text or ""
    if len(body) > 400:
        body = body[:400] + "…"
    return f"Granola API error: HTTP {resp.status_code} — {body}"


# -------- fake meeting source (test-only; toggled by tools.granola.fakeMeetings) --------
# ponytail: a gibberish generator behind the single Granola read seam, so the
# meeting-classifier pipeline can be exercised without real Granola notes. The
# batch is generated once and cached, so the poller (which dedups by note id)
# processes each fake meeting exactly once and then goes quiet.
_FAKE_NOTES: dict[str, dict[str, Any]] | None = None
_FAKE_WORDS = (
    "sync roadmap retro latency budget onboarding metrics rollout backlog cadence "
    "sprint blockers staging incident handoff scope demo intake velocity refactor "
    "prototype telemetry migration outage regression cutover baseline standup"
).split()


def _fake_words(n: int) -> str:
    return " ".join(random.choice(_FAKE_WORDS) for _ in range(n))


def _make_fake_notes(count: int, folder_id: str) -> dict[str, dict[str, Any]]:
    now = datetime.now(timezone.utc).isoformat()
    notes: dict[str, dict[str, Any]] = {}
    for _ in range(max(1, count)):
        nid = "not_fake_" + random.randbytes(6).hex()
        people = [w.title() for w in _fake_words(2).split()]
        notes[nid] = {
            "id": nid,
            "title": _fake_words(4).title(),
            "summary": _fake_words(20),
            "transcript": ". ".join(_fake_words(12) for _ in range(6)),
            "attendees": [{"name": p, "email": p.lower() + "@example.com"} for p in people],
            "folder_id": folder_id,
            "created_at": now,
        }
    return notes


def _fake_granola(path: str, params: dict[str, Any] | None, count: int) -> dict[str, Any]:
    global _FAKE_NOTES
    if _FAKE_NOTES is None:
        _FAKE_NOTES = _make_fake_notes(count, (params or {}).get("folder_id", ""))
    if path == "/notes":
        return {"notes": list(_FAKE_NOTES.values())}
    if path.startswith("/notes/"):
        note = _FAKE_NOTES.get(path[len("/notes/") :])
        return note if note is not None else {"id": "", "title": "", "transcript": ""}
    if path == "/folders":
        return {"folders": []}
    return {"notes": []}


async def _granola_get(
    cfg: GranolaToolConfig,
    path: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any] | str:
    """GET against the Granola API. Returns parsed JSON on 2xx, error string otherwise."""
    if cfg.fake_meetings:
        return _fake_granola(path, params, cfg.fake_meetings)
    if not cfg.api_key:
        return "Granola API error: api_key is not configured"
    url = f"{cfg.base_url.rstrip('/')}{path}"
    headers = {
        "Authorization": f"Bearer {cfg.api_key}",
        "Accept": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=cfg.timeout) as client:
            resp = await client.get(url, params=params or None, headers=headers)
    except httpx.RequestError as exc:
        return f"Granola API error: request failed — {exc.__class__.__name__}: {exc}"
    if resp.status_code == 429:
        retry = resp.headers.get("Retry-After", "?")
        return f"Granola API error: rate limited (HTTP 429, retry-after={retry}s)"
    if resp.status_code >= 400:
        return _format_http_error(resp)
    try:
        return resp.json()
    except ValueError:
        return f"Granola API error: response was not JSON (HTTP {resp.status_code})"


# -------- list notes --------


@tool_parameters(
    tool_parameters_schema(
        folder_id=StringSchema(
            "Granola folder ID (e.g. 'fld_...'). When set, only notes in this folder are returned. "
            "Use this to scope to a project (see metadata.project.granola.folder_id).",
        ),
        created_after=StringSchema(
            "ISO 8601 timestamp (e.g. '2026-05-20T00:00:00Z'). Returns notes created at or after this time.",
        ),
        cursor=StringSchema(
            "Opaque pagination cursor from a previous response. Pass to fetch the next page.",
        ),
        limit=IntegerSchema(
            50,
            description="Max notes per page (1-100). Granola's default is provider-side.",
            minimum=1,
            maximum=100,
        ),
        required=[],
    )
)
class GranolaListNotesTool(Tool):
    """List Granola meeting notes (scoped, paginated)."""

    _scopes = {"core", "subagent"}
    name = "granola_list_notes"
    description = (
        "List Granola meeting notes. Filter by folder_id (project scope) and/or created_after. "
        "Returns notes that have a generated AI summary + transcript only. Use cursor for pagination."
    )
    config_key = "granola"

    @property
    def read_only(self) -> bool:
        return True

    @classmethod
    def config_cls(cls) -> type[GranolaToolConfig]:
        return GranolaToolConfig

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        cfg = ctx.config.granola
        return bool(cfg.enable and cfg.api_key)

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(config=ctx.config.granola)

    def __init__(self, config: GranolaToolConfig | None = None) -> None:
        self.config = config if config is not None else GranolaToolConfig()

    async def execute(self, **kwargs: Any) -> str:
        params: dict[str, Any] = {}
        if v := kwargs.get("folder_id"):
            params["folder_id"] = v
        if v := kwargs.get("created_after"):
            params["created_after"] = v
        if v := kwargs.get("cursor"):
            params["cursor"] = v
        if v := kwargs.get("limit"):
            params["limit"] = v
        result = await _granola_get(self.config, "/notes", params=params)
        if isinstance(result, str):
            return result
        return json.dumps(result, indent=2)


# -------- get note --------


@tool_parameters(
    tool_parameters_schema(
        note_id=StringSchema(
            "Granola note ID (e.g. 'not_...'). Get it from granola_list_notes — do NOT use UUIDs.",
        ),
        include_transcript=BooleanSchema(
            description="If true (default), include the full transcript in the response.",
            default=True,
        ),
        required=["note_id"],
    )
)
class GranolaGetNoteTool(Tool):
    """Fetch a single Granola meeting note (with optional transcript)."""

    _scopes = {"core", "subagent"}
    name = "granola_get_note"
    description = (
        "Get one Granola meeting note by ID. Returns title, summary, attendees, calendar event, "
        "folder membership, and (when include_transcript is true) the full diarized transcript. "
        "Returns 404 if the note has no generated summary/transcript yet."
    )
    config_key = "granola"

    @property
    def read_only(self) -> bool:
        return True

    @classmethod
    def config_cls(cls) -> type[GranolaToolConfig]:
        return GranolaToolConfig

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        cfg = ctx.config.granola
        return bool(cfg.enable and cfg.api_key)

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(config=ctx.config.granola)

    def __init__(self, config: GranolaToolConfig | None = None) -> None:
        self.config = config if config is not None else GranolaToolConfig()

    async def execute(self, **kwargs: Any) -> str:
        note_id = kwargs.get("note_id")
        if not note_id:
            return "Granola API error: note_id is required"
        params: dict[str, Any] = {}
        if kwargs.get("include_transcript", True):
            params["include"] = "transcript"
        result = await _granola_get(self.config, f"/notes/{note_id}", params=params)
        if isinstance(result, str):
            return result
        return json.dumps(result, indent=2)


# -------- list folders --------


@tool_parameters(
    tool_parameters_schema(
        cursor=StringSchema(
            "Opaque pagination cursor from a previous response. Pass to fetch the next page.",
        ),
        required=[],
    )
)
class GranolaListFoldersTool(Tool):
    """List Granola folders (workspace organizational unit)."""

    _scopes = {"core", "subagent"}
    name = "granola_list_folders"
    description = (
        "List Granola folders visible to the API key, sorted alphabetically. "
        "Each folder may include parent_folder_id for hierarchy. Use folder.id as folder_id "
        "elsewhere when scoping a project."
    )
    config_key = "granola"

    @property
    def read_only(self) -> bool:
        return True

    @classmethod
    def config_cls(cls) -> type[GranolaToolConfig]:
        return GranolaToolConfig

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        cfg = ctx.config.granola
        return bool(cfg.enable and cfg.api_key)

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(config=ctx.config.granola)

    def __init__(self, config: GranolaToolConfig | None = None) -> None:
        self.config = config if config is not None else GranolaToolConfig()

    async def execute(self, **kwargs: Any) -> str:
        params: dict[str, Any] = {}
        if v := kwargs.get("cursor"):
            params["cursor"] = v
        result = await _granola_get(self.config, "/folders", params=params)
        if isinstance(result, str):
            return result
        return json.dumps(result, indent=2)


__all__ = [
    "GranolaToolConfig",
    "GranolaListNotesTool",
    "GranolaGetNoteTool",
    "GranolaListFoldersTool",
]
