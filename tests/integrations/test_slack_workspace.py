from __future__ import annotations

from types import SimpleNamespace

import pytest
from slack_sdk.errors import SlackApiError

from nanobot.integrations.slack_workspace import (
    SlackAmbiguousError,
    SlackPermanentError,
    SlackWorkspaceClient,
)


class FakeSlackClient:
    def __init__(self) -> None:
        self.user_pages: list[dict] = []
        self.channel_pages: list[dict] = []
        self.calls: list[tuple[str, dict]] = []
        self.create_error: Exception | None = None

    async def users_list(self, **kwargs):
        self.calls.append(("users_list", kwargs))
        return self.user_pages.pop(0)

    async def conversations_list(self, **kwargs):
        self.calls.append(("conversations_list", kwargs))
        return self.channel_pages.pop(0)

    async def conversations_create(self, **kwargs):
        self.calls.append(("conversations_create", kwargs))
        if self.create_error:
            raise self.create_error
        return {"channel": {"id": "CNEW", "name": kwargs["name"]}}

    async def conversations_setTopic(self, **kwargs):  # noqa: N802 - Slack SDK name
        self.calls.append(("conversations_setTopic", kwargs))
        return {"ok": True}

    async def conversations_invite(self, **kwargs):
        self.calls.append(("conversations_invite", kwargs))
        return {"ok": True}

    async def conversations_open(self, **kwargs):
        self.calls.append(("conversations_open", kwargs))
        return {"channel": {"id": "D1"}}

    async def chat_postMessage(self, **kwargs):  # noqa: N802 - Slack SDK name
        self.calls.append(("chat_postMessage", kwargs))
        return {"ts": "1.2"}

    async def chat_update(self, **kwargs):
        self.calls.append(("chat_update", kwargs))
        return {"ok": True}

    async def views_open(self, **kwargs):
        self.calls.append(("views_open", kwargs))
        return {"ok": True}


@pytest.mark.asyncio
async def test_resolve_user_paginates_and_requires_one_exact_active_human() -> None:
    fake = FakeSlackClient()
    fake.user_pages = [
        {
            "members": [
                {"id": "UBOT", "is_bot": True, "profile": {"email": "ash@example.edu"}},
                {"id": "UDEL", "deleted": True, "profile": {"email": "ash@example.edu"}},
            ],
            "response_metadata": {"next_cursor": "next"},
        },
        {
            "members": [
                {
                    "id": "U1",
                    "deleted": False,
                    "is_bot": False,
                    "profile": {"email": " Ash@Example.edu ", "real_name": "Ash"},
                }
            ],
            "response_metadata": {"next_cursor": ""},
        },
    ]
    user = await SlackWorkspaceClient(lambda: fake).resolve_user_by_email("ash@example.edu")
    assert (user.user_id, user.name, user.email) == ("U1", "Ash", "ash@example.edu")
    assert [call[1]["cursor"] for call in fake.calls] == ["", "next"]


@pytest.mark.asyncio
async def test_channel_slug_creation_and_exact_marker_reconciliation() -> None:
    fake = FakeSlackClient()
    adapter = SlackWorkspaceClient(lambda: fake)
    created = await adapter.create_public_channel("  New___Project !!! ")
    assert created.channel_id == "CNEW"
    assert fake.calls[-1] == (
        "conversations_create",
        {"name": "new-project", "is_private": False},
    )

    await adapter.set_channel_marker("CNEW", "projectclaw:project:new-project")
    assert fake.calls[-1][1]["topic"] == "projectclaw:project:new-project"

    fake.channel_pages = [
        {
            "channels": [
                {
                    "id": "CNEW",
                    "name": "new-project",
                    "topic": {"value": "owner projectclaw:project:new-project"},
                    "purpose": {"value": ""},
                }
            ],
            "response_metadata": {"next_cursor": ""},
        }
    ]
    found = await adapter.find_channel_by_slug("new-project")
    assert found is not None
    assert found.marker == ""


@pytest.mark.asyncio
async def test_name_taken_requires_projectclaw_marker() -> None:
    fake = FakeSlackClient()
    response = SimpleNamespace(status_code=400, headers={}, data={"error": "name_taken"})
    fake.create_error = SlackApiError("raw secret", response)
    fake.channel_pages = [
        {
            "channels": [
                {"id": "C1", "name": "atlas", "topic": {"value": "unowned"}},
            ],
            "response_metadata": {"next_cursor": ""},
        }
    ]
    with pytest.raises(SlackAmbiguousError):
        await SlackWorkspaceClient(lambda: fake).create_public_channel("atlas")


@pytest.mark.asyncio
async def test_invites_only_existing_users_and_rich_operations() -> None:
    fake = FakeSlackClient()
    fake.user_pages = [
        {
            "members": [
                {"id": "U1", "profile": {"email": "one@example.edu"}},
                {"id": "U2", "profile": {"email": "two@example.edu"}},
            ],
            "response_metadata": {"next_cursor": ""},
        }
    ]
    adapter = SlackWorkspaceClient(lambda: fake)
    await adapter.invite_users("C1", ["U2", "U1", "U2"])
    assert fake.calls[-1] == ("conversations_invite", {"channel": "C1", "users": "U1,U2"})
    assert await adapter.open_dm("U1") == "D1"
    assert await adapter.post_blocks("C1", "fallback", [{"type": "section"}]) == "1.2"
    await adapter.update_blocks("C1", "1.2", "fallback", [])
    await adapter.open_modal("trigger", {"type": "modal"})


@pytest.mark.asyncio
async def test_missing_client_and_unknown_invitee_are_safe_permanent_errors() -> None:
    with pytest.raises(SlackPermanentError, match="not ready") as exc_info:
        await SlackWorkspaceClient(lambda: None).open_dm("U1")
    assert exc_info.value.__context__ is None

    fake = FakeSlackClient()
    fake.user_pages = [{"members": [], "response_metadata": {"next_cursor": ""}}]
    with pytest.raises(SlackPermanentError, match="workspace user"):
        await SlackWorkspaceClient(lambda: fake).invite_users("C1", ["U404"])
