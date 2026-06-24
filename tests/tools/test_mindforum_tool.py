"""Tests for the MindForum programmatic-API tools.

These tests mock httpx.AsyncClient with a transport-level handler (no network).
The point is to pin:

  1. The request shape: URL, Bearer header, JSON body (camelCase per the API).
  2. The error surface: 4xx/429/transport-failure become structured error
     strings (not exceptions), matching the granola-tool convention.
  3. Client-side slug validation and invite-count guard fire before the wire.
  4. Inert until host + api_key are configured (the `active` gate).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Callable

import httpx
import pytest

import nanobot.agent.tools.mindforum as mindforum_mod
from nanobot.agent.tools.mindforum import (
    CreateMindForumRoomTool,
    InviteToMindForumRoomTool,
    MindForumToolConfig,
)


def _install_handler(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[httpx.Request]:
    """Patch httpx.AsyncClient inside mindforum_mod to use a MockTransport.

    Returns a list that captures every request the handler saw, in order.
    """
    seen: list[httpx.Request] = []

    def recording_handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    transport = httpx.MockTransport(recording_handler)
    real = httpx.AsyncClient

    class TransportAsyncClient(real):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(mindforum_mod.httpx, "AsyncClient", TransportAsyncClient)
    return seen


def _cfg(host: str = "https://forum.example.com", api_key: str = "mf_sk_test") -> MindForumToolConfig:
    return MindForumToolConfig(enabled=True, host=host, api_key=api_key)


# ---------- create room: success paths ----------


@pytest.mark.asyncio
async def test_create_room_sends_bearer_and_camelcase_body(monkeypatch):
    seen = _install_handler(
        monkeypatch,
        lambda req: httpx.Response(201, json={"id": "my-room", "name": "My room"}),
    )
    tool = CreateMindForumRoomTool(config=_cfg())
    result = await tool.execute(name="My room", slug="my-room")

    assert "Created MindForum room" in result
    assert "my-room" in result
    assert "My room" in result

    assert len(seen) == 1
    req = seen[0]
    assert req.method == "POST"
    assert req.url.path == "/api/v1/rooms"
    assert req.headers["authorization"] == "Bearer mf_sk_test"
    assert req.headers["content-type"] == "application/json"
    body = json.loads(req.content)
    assert body == {"name": "My room", "id": "my-room"}


@pytest.mark.asyncio
async def test_create_room_with_system_prompt_uses_camel_case(monkeypatch):
    seen = _install_handler(
        monkeypatch,
        lambda req: httpx.Response(201, json={"id": "auto-id", "name": "Critique"}),
    )
    tool = CreateMindForumRoomTool(config=_cfg())
    await tool.execute(name="Critique", system_prompt="Be a kind critic.")

    body = json.loads(seen[0].content)
    assert body == {"name": "Critique", "systemPrompt": "Be a kind critic."}


@pytest.mark.asyncio
async def test_create_room_auto_generates_slug_when_omitted(monkeypatch):
    seen = _install_handler(
        monkeypatch,
        lambda req: httpx.Response(201, json={"id": "gen-abc", "name": "Sync"}),
    )
    tool = CreateMindForumRoomTool(config=_cfg())
    result = await tool.execute(name="Sync")

    body = json.loads(seen[0].content)
    assert "id" not in body
    assert body == {"name": "Sync"}
    assert "gen-abc" in result


# ---------- create room: client-side guards ----------


@pytest.mark.asyncio
async def test_create_room_rejects_invalid_slug_before_http(monkeypatch):
    seen = _install_handler(monkeypatch, lambda req: httpx.Response(201, json={}))
    tool = CreateMindForumRoomTool(config=_cfg())
    result = await tool.execute(name="Bad", slug="UPPER CASE!")

    assert "invalid slug" in result
    assert seen == []  # never hit the wire


@pytest.mark.asyncio
async def test_create_room_requires_name():
    tool = CreateMindForumRoomTool(config=_cfg())
    result = await tool.execute()
    assert "name is required" in result


# ---------- create room: error surface ----------


@pytest.mark.asyncio
async def test_create_room_slug_taken_surfaces_409(monkeypatch):
    _install_handler(monkeypatch, lambda req: httpx.Response(409, json={"error": "slug_taken"}))
    tool = CreateMindForumRoomTool(config=_cfg())
    result = await tool.execute(name="X", slug="taken")
    assert "MindForum API error" in result
    assert "409" in result
    assert "slug_taken" in result


@pytest.mark.asyncio
async def test_create_room_system_prompt_too_long_surfaces_400(monkeypatch):
    _install_handler(
        monkeypatch,
        lambda req: httpx.Response(400, json={"error": "system_prompt_too_long"}),
    )
    tool = CreateMindForumRoomTool(config=_cfg())
    result = await tool.execute(name="X", system_prompt="long" * 10000)
    assert "400" in result
    assert "system_prompt_too_long" in result


@pytest.mark.asyncio
async def test_create_room_429_surfaces_rate_limit(monkeypatch):
    _install_handler(
        monkeypatch,
        lambda req: httpx.Response(429, headers={"Retry-After": "5"}, json={"error": "rate_limited"}),
    )
    tool = CreateMindForumRoomTool(config=_cfg())
    result = await tool.execute(name="X")
    assert "rate limited" in result.lower()
    assert "429" in result
    assert "retry-after=5" in result


@pytest.mark.asyncio
async def test_create_room_transport_failure_returns_structured_error(monkeypatch):
    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=req)

    _install_handler(monkeypatch, boom)
    tool = CreateMindForumRoomTool(config=_cfg())
    result = await tool.execute(name="X")
    assert "request failed" in result.lower()
    assert "ConnectError" in result


# ---------- invite: success paths ----------


@pytest.mark.asyncio
async def test_invite_uses_bulk_body_and_camelcase_fields(monkeypatch):
    seen = _install_handler(
        monkeypatch,
        lambda req: httpx.Response(201, json={"created": [{"id": "inv_1"}, {"id": "inv_2"}], "skipped": 0}),
    )
    tool = InviteToMindForumRoomTool(config=_cfg())
    result = await tool.execute(
        room_id="weekly-sync",
        invites=[
            {"invitee_email": "a@x.edu", "invitee_name": "Avery"},
            {"invitee_email": "b@x.edu", "invitee_name": "Blair"},
        ],
    )

    assert "Invited 2" in result
    assert "weekly-sync" in result
    assert "skipped" not in result  # skipped == 0 → omitted

    assert len(seen) == 1
    req = seen[0]
    assert req.method == "POST"
    assert req.url.path == "/api/v1/rooms/weekly-sync/invitations"
    assert req.headers["authorization"] == "Bearer mf_sk_test"
    body = json.loads(req.content)
    assert body == {
        "invites": [
            {"inviteeEmail": "a@x.edu", "inviteeName": "Avery"},
            {"inviteeEmail": "b@x.edu", "inviteeName": "Blair"},
        ]
    }


@pytest.mark.asyncio
async def test_invite_reports_skipped_duplicates(monkeypatch):
    _install_handler(
        monkeypatch,
        lambda req: httpx.Response(201, json={"created": [{"id": "inv_1"}], "skipped": 1}),
    )
    tool = InviteToMindForumRoomTool(config=_cfg())
    result = await tool.execute(
        room_id="r",
        invites=[{"invitee_email": "a@x.edu", "invitee_name": "A"}],
    )
    assert "Invited 1" in result
    assert "skipped: 1" in result


# ---------- invite: client-side guards ----------


@pytest.mark.asyncio
async def test_invite_requires_room_id(monkeypatch):
    seen = _install_handler(monkeypatch, lambda req: httpx.Response(201, json={}))
    tool = InviteToMindForumRoomTool(config=_cfg())
    result = await tool.execute(invites=[{"invitee_email": "a@x.edu", "invitee_name": "A"}])
    assert "room_id is required" in result
    assert seen == []


@pytest.mark.asyncio
async def test_invite_requires_non_empty_invites(monkeypatch):
    seen = _install_handler(monkeypatch, lambda req: httpx.Response(201, json={}))
    tool = InviteToMindForumRoomTool(config=_cfg())
    result = await tool.execute(room_id="r", invites=[])
    assert "invites is required" in result
    assert seen == []


@pytest.mark.asyncio
async def test_invite_rejects_over_50_before_http(monkeypatch):
    seen = _install_handler(monkeypatch, lambda req: httpx.Response(201, json={}))
    tool = InviteToMindForumRoomTool(config=_cfg())
    too_many = [{"invitee_email": f"u{i}@x.edu", "invitee_name": f"U{i}"} for i in range(51)]
    result = await tool.execute(room_id="r", invites=too_many)
    assert "too many invites" in result
    assert seen == []


# ---------- invite: error surface ----------


@pytest.mark.asyncio
async def test_invite_not_found_when_room_not_owned(monkeypatch):
    _install_handler(monkeypatch, lambda req: httpx.Response(404, json={"error": "not_found"}))
    tool = InviteToMindForumRoomTool(config=_cfg())
    result = await tool.execute(room_id="other", invites=[{"invitee_email": "a@x.edu", "invitee_name": "A"}])
    assert "404" in result
    assert "not_found" in result


@pytest.mark.asyncio
async def test_invite_429_surfaces_rate_limit(monkeypatch):
    _install_handler(
        monkeypatch,
        lambda req: httpx.Response(429, headers={"Retry-After": "2"}, json={"error": "rate_limited"}),
    )
    tool = InviteToMindForumRoomTool(config=_cfg())
    result = await tool.execute(room_id="r", invites=[{"invitee_email": "a@x.edu", "invitee_name": "A"}])
    assert "rate limited" in result.lower()
    assert "retry-after=2" in result


# ---------- enablement / registration ----------


@pytest.mark.parametrize("cls", [CreateMindForumRoomTool, InviteToMindForumRoomTool])
def test_disabled_when_not_active(cls):
    ctx = SimpleNamespace(config=SimpleNamespace(mindforum=MindForumToolConfig()))
    assert cls.enabled(ctx) is False


@pytest.mark.parametrize("cls", [CreateMindForumRoomTool, InviteToMindForumRoomTool])
def test_disabled_when_enabled_but_no_host(cls):
    ctx = SimpleNamespace(
        config=SimpleNamespace(mindforum=MindForumToolConfig(enabled=True, api_key="mf_sk_x"))
    )
    assert cls.enabled(ctx) is False


@pytest.mark.parametrize("cls", [CreateMindForumRoomTool, InviteToMindForumRoomTool])
def test_disabled_when_enabled_but_no_api_key(cls):
    ctx = SimpleNamespace(
        config=SimpleNamespace(mindforum=MindForumToolConfig(enabled=True, host="https://forum.example.com"))
    )
    assert cls.enabled(ctx) is False


@pytest.mark.parametrize("cls", [CreateMindForumRoomTool, InviteToMindForumRoomTool])
def test_enabled_when_active(cls):
    ctx = SimpleNamespace(
        config=SimpleNamespace(
            mindforum=MindForumToolConfig(enabled=True, host="https://forum.example.com", api_key="mf_sk_x")
        )
    )
    assert cls.enabled(ctx) is True


@pytest.mark.asyncio
async def test_inactive_config_returns_error_without_http(monkeypatch):
    seen = _install_handler(monkeypatch, lambda req: httpx.Response(201, json={}))
    tool = CreateMindForumRoomTool(config=MindForumToolConfig())  # inert
    result = await tool.execute(name="X")
    assert "not configured" in result.lower()
    assert seen == []
