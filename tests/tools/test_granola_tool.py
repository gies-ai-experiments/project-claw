"""Tests for the Granola public-API tools.

These tests mock httpx.AsyncClient with a transport-level handler (no network).
The point is to pin:

  1. The request shape: URL, Bearer header, params, JSON body.
  2. The error surface: 4xx/5xx/429/transport-failure become structured
     error strings (not exceptions). This keeps the projectclaw skill's
     'partial-answer on tool failure' rule honest.
  3. Disable when api_key is missing (so the tool doesn't try a doomed call).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Callable

import httpx
import pytest

import nanobot.agent.tools.granola as granola_mod
from nanobot.agent.tools.granola import (
    GranolaGetNoteTool,
    GranolaListFoldersTool,
    GranolaListNotesTool,
    GranolaToolConfig,
)


def _install_handler(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[httpx.Request]:
    """Patch httpx.AsyncClient inside granola_mod to use a MockTransport.

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

    monkeypatch.setattr(granola_mod.httpx, "AsyncClient", TransportAsyncClient)
    return seen


def _cfg(api_key: str = "grn_testkey") -> GranolaToolConfig:
    return GranolaToolConfig(api_key=api_key)


# ---------- success paths ----------


@pytest.mark.asyncio
async def test_list_notes_sends_bearer_and_query_params(monkeypatch):
    seen = _install_handler(
        monkeypatch,
        lambda req: httpx.Response(200, json={"notes": [{"id": "not_1", "title": "ok"}], "hasMore": False}),
    )
    tool = GranolaListNotesTool(config=_cfg())
    result = await tool.execute(
        folder_id="fld_abc",
        created_after="2026-05-20T00:00:00Z",
        limit=25,
    )
    parsed = json.loads(result)
    assert parsed["notes"][0]["id"] == "not_1"

    assert len(seen) == 1
    req = seen[0]
    assert req.method == "GET"
    assert req.url.path == "/v1/notes"
    assert req.url.params["folder_id"] == "fld_abc"
    assert req.url.params["created_after"] == "2026-05-20T00:00:00Z"
    assert req.url.params["limit"] == "25"
    assert req.headers["authorization"] == "Bearer grn_testkey"
    assert req.headers["accept"] == "application/json"


@pytest.mark.asyncio
async def test_get_note_passes_include_transcript_and_id(monkeypatch):
    seen = _install_handler(
        monkeypatch,
        lambda req: httpx.Response(200, json={"id": "not_x", "summary": "hi", "transcript": []}),
    )
    tool = GranolaGetNoteTool(config=_cfg())
    out = await tool.execute(note_id="not_x", include_transcript=True)
    assert "transcript" in out

    req = seen[0]
    assert req.url.path == "/v1/notes/not_x"
    assert req.url.params["include"] == "transcript"


@pytest.mark.asyncio
async def test_get_note_can_omit_transcript(monkeypatch):
    seen = _install_handler(
        monkeypatch,
        lambda req: httpx.Response(200, json={"id": "not_y", "summary": "x"}),
    )
    tool = GranolaGetNoteTool(config=_cfg())
    await tool.execute(note_id="not_y", include_transcript=False)
    assert "include" not in seen[0].url.params


@pytest.mark.asyncio
async def test_list_folders_no_params(monkeypatch):
    seen = _install_handler(
        monkeypatch,
        lambda req: httpx.Response(200, json={"folders": [{"id": "fld_a", "name": "Alpha"}]}),
    )
    tool = GranolaListFoldersTool(config=_cfg())
    out = await tool.execute()
    assert "Alpha" in out
    assert seen[0].url.path == "/v1/folders"


# ---------- error surface ----------


@pytest.mark.asyncio
async def test_missing_api_key_returns_error_does_not_call_http(monkeypatch):
    seen = _install_handler(monkeypatch, lambda req: httpx.Response(200, json={}))
    tool = GranolaListNotesTool(config=GranolaToolConfig(api_key=""))
    out = await tool.execute()
    assert "api_key" in out.lower()
    assert seen == []  # never hit the wire


@pytest.mark.asyncio
async def test_http_429_surfaces_rate_limit_message(monkeypatch):
    _install_handler(
        monkeypatch,
        lambda req: httpx.Response(429, headers={"Retry-After": "3"}, json={"error": "slow down"}),
    )
    out = await GranolaListNotesTool(config=_cfg()).execute()
    assert "rate limited" in out.lower()
    assert "429" in out
    assert "retry-after=3" in out


@pytest.mark.asyncio
async def test_http_4xx_returns_structured_error_with_body(monkeypatch):
    _install_handler(
        monkeypatch,
        lambda req: httpx.Response(401, json={"error": "Invalid API key"}),
    )
    out = await GranolaListFoldersTool(config=_cfg()).execute()
    assert "Granola API error" in out
    assert "401" in out
    assert "Invalid API key" in out


@pytest.mark.asyncio
async def test_transport_failure_returns_structured_error(monkeypatch):
    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=req)

    _install_handler(monkeypatch, boom)
    out = await GranolaListNotesTool(config=_cfg()).execute()
    assert "request failed" in out.lower()
    assert "ConnectError" in out


# ---------- enablement / registration ----------


@pytest.mark.parametrize("cls", [GranolaListNotesTool, GranolaGetNoteTool, GranolaListFoldersTool])
def test_disabled_when_api_key_missing(cls):
    ctx = SimpleNamespace(config=SimpleNamespace(granola=GranolaToolConfig(api_key="")))
    assert cls.enabled(ctx) is False


@pytest.mark.parametrize("cls", [GranolaListNotesTool, GranolaGetNoteTool, GranolaListFoldersTool])
def test_enabled_when_api_key_present(cls):
    ctx = SimpleNamespace(config=SimpleNamespace(granola=GranolaToolConfig(api_key="grn_x")))
    assert cls.enabled(ctx) is True


@pytest.mark.parametrize("cls", [GranolaListNotesTool, GranolaGetNoteTool, GranolaListFoldersTool])
def test_disabled_when_enable_false(cls):
    ctx = SimpleNamespace(config=SimpleNamespace(granola=GranolaToolConfig(api_key="grn_x", enable=False)))
    assert cls.enabled(ctx) is False
