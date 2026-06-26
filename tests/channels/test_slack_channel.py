from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

# Check optional Slack dependencies before running tests
try:
    import slack_sdk  # noqa: F401
except ImportError:
    pytest.skip("Slack dependencies not installed (slack-sdk)", allow_module_level=True)

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.slack import SLACK_MAX_MESSAGE_LEN, SlackChannel, SlackConfig


class _FakeAsyncWebClient:
    def __init__(self) -> None:
        self.chat_post_calls: list[dict[str, object | None]] = []
        self.chat_update_calls: list[dict[str, object | None]] = []
        self.chat_delete_calls: list[dict[str, object | None]] = []
        self.file_upload_calls: list[dict[str, object | None]] = []
        self.reactions_add_calls: list[dict[str, object | None]] = []
        self.reactions_remove_calls: list[dict[str, object | None]] = []
        self.conversations_list_calls: list[dict[str, object | None]] = []
        self.conversations_replies_calls: list[dict[str, object | None]] = []
        self.users_list_calls: list[dict[str, object | None]] = []
        self.conversations_open_calls: list[dict[str, object | None]] = []
        self._conversations_pages: list[dict[str, object]] = []
        self._conversations_replies_response: dict[str, object] = {"messages": []}
        self._users_pages: list[dict[str, object]] = []
        self._open_dm_response: dict[str, object] = {"channel": {"id": "D_OPENED"}}
        self._post_counter = 0

    async def chat_postMessage(  # noqa: N802 - mirrors Slack SDK method name
        self,
        *,
        channel: str,
        text: str,
        thread_ts: str | None = None,
        blocks: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        call: dict[str, object | None] = {
            "channel": channel,
            "text": text,
            "thread_ts": thread_ts,
        }
        if blocks is not None:
            call["blocks"] = blocks
        self.chat_post_calls.append(call)
        self._post_counter += 1
        return {"ts": f"ts.{self._post_counter:04d}"}

    async def chat_update(  # noqa: N802 - mirrors Slack SDK method name
        self,
        *,
        channel: str,
        ts: str,
        text: str,
        blocks: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        call: dict[str, object | None] = {"channel": channel, "ts": ts, "text": text}
        if blocks is not None:
            call["blocks"] = blocks
        self.chat_update_calls.append(call)
        return {"ts": ts}

    async def chat_delete(  # noqa: N802 - mirrors Slack SDK method name
        self,
        *,
        channel: str,
        ts: str,
    ) -> dict[str, object]:
        self.chat_delete_calls.append({"channel": channel, "ts": ts})
        return {"ok": True}

    async def files_upload_v2(
        self,
        *,
        channel: str,
        file: str,
        thread_ts: str | None = None,
    ) -> None:
        self.file_upload_calls.append(
            {
                "channel": channel,
                "file": file,
                "thread_ts": thread_ts,
            }
        )

    async def reactions_add(
        self,
        *,
        channel: str,
        name: str,
        timestamp: str,
    ) -> None:
        self.reactions_add_calls.append(
            {
                "channel": channel,
                "name": name,
                "timestamp": timestamp,
            }
        )

    async def reactions_remove(
        self,
        *,
        channel: str,
        name: str,
        timestamp: str,
    ) -> None:
        self.reactions_remove_calls.append(
            {
                "channel": channel,
                "name": name,
                "timestamp": timestamp,
            }
        )

    async def conversations_list(self, **kwargs):
        self.conversations_list_calls.append(kwargs)
        if self._conversations_pages:
            return self._conversations_pages.pop(0)
        return {"channels": [], "response_metadata": {"next_cursor": ""}}

    async def conversations_replies(self, **kwargs):
        self.conversations_replies_calls.append(kwargs)
        return self._conversations_replies_response

    async def users_list(self, **kwargs):
        self.users_list_calls.append(kwargs)
        if self._users_pages:
            return self._users_pages.pop(0)
        return {"members": [], "response_metadata": {"next_cursor": ""}}

    async def conversations_open(self, **kwargs):
        self.conversations_open_calls.append(kwargs)
        return self._open_dm_response


@pytest.mark.asyncio
async def test_send_uses_thread_for_channel_messages() -> None:
    channel = SlackChannel(SlackConfig(enabled=True), MessageBus())
    fake_web = _FakeAsyncWebClient()
    channel._web_client = fake_web

    await channel.send(
        OutboundMessage(
            channel="slack",
            chat_id="C123",
            content="hello",
            media=["/tmp/demo.txt"],
            metadata={"slack": {"thread_ts": "1700000000.000100", "channel_type": "channel"}},
        )
    )

    assert len(fake_web.chat_post_calls) == 1
    assert fake_web.chat_post_calls[0]["text"] == "hello"
    assert fake_web.chat_post_calls[0]["thread_ts"] == "1700000000.000100"
    assert len(fake_web.file_upload_calls) == 1
    assert fake_web.file_upload_calls[0]["thread_ts"] == "1700000000.000100"


@pytest.mark.asyncio
async def test_send_omits_thread_for_dm_root_messages() -> None:
    """DM root replies should not be threaded; metadata carries thread_ts=None."""
    channel = SlackChannel(SlackConfig(enabled=True), MessageBus())
    fake_web = _FakeAsyncWebClient()
    channel._web_client = fake_web

    await channel.send(
        OutboundMessage(
            channel="slack",
            chat_id="D123",
            content="hello",
            media=["/tmp/demo.txt"],
            metadata={"slack": {"thread_ts": None, "channel_type": "im"}},
        )
    )

    assert len(fake_web.chat_post_calls) == 1
    assert fake_web.chat_post_calls[0]["text"] == "hello"
    assert fake_web.chat_post_calls[0]["thread_ts"] is None
    assert len(fake_web.file_upload_calls) == 1
    assert fake_web.file_upload_calls[0]["thread_ts"] is None


@pytest.mark.asyncio
async def test_send_keeps_thread_for_dm_thread_messages() -> None:
    """When the user replies inside a DM thread, bot replies stay in the same thread."""
    channel = SlackChannel(SlackConfig(enabled=True), MessageBus())
    fake_web = _FakeAsyncWebClient()
    channel._web_client = fake_web

    await channel.send(
        OutboundMessage(
            channel="slack",
            chat_id="D123",
            content="hello",
            media=["/tmp/demo.txt"],
            metadata={
                "slack": {
                    "thread_ts": "1700000000.000100",
                    "channel_type": "im",
                    "event": {"channel": "D123"},
                }
            },
        )
    )

    assert len(fake_web.chat_post_calls) == 1
    assert fake_web.chat_post_calls[0]["thread_ts"] == "1700000000.000100"
    assert len(fake_web.file_upload_calls) == 1
    assert fake_web.file_upload_calls[0]["thread_ts"] == "1700000000.000100"


@pytest.mark.asyncio
async def test_send_splits_long_messages() -> None:
    channel = SlackChannel(SlackConfig(enabled=True), MessageBus())
    fake_web = _FakeAsyncWebClient()
    channel._web_client = fake_web

    await channel.send(
        OutboundMessage(
            channel="slack",
            chat_id="C123",
            content="x" * (SLACK_MAX_MESSAGE_LEN + 10),
        )
    )

    assert len(fake_web.chat_post_calls) == 2
    assert all(len(str(call["text"])) <= SLACK_MAX_MESSAGE_LEN for call in fake_web.chat_post_calls)


@pytest.mark.asyncio
async def test_send_renders_buttons_on_last_message_chunk() -> None:
    channel = SlackChannel(SlackConfig(enabled=True), MessageBus())
    fake_web = _FakeAsyncWebClient()
    channel._web_client = fake_web

    await channel.send(
        OutboundMessage(
            channel="slack",
            chat_id="C123",
            content="Choose one",
            buttons=[["Yes", "No"]],
        )
    )

    assert len(fake_web.chat_post_calls) == 1
    blocks = fake_web.chat_post_calls[0]["blocks"]
    assert isinstance(blocks, list)
    assert blocks[-1] == {
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Yes"},
                "value": "Yes",
                "action_id": "btn_Yes",
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "No"},
                "value": "No",
                "action_id": "btn_No",
            },
        ],
    }


@pytest.mark.asyncio
async def test_send_updates_reaction_when_final_response_sent() -> None:
    channel = SlackChannel(SlackConfig(enabled=True, react_emoji="eyes"), MessageBus())
    fake_web = _FakeAsyncWebClient()
    channel._web_client = fake_web

    await channel.send(
        OutboundMessage(
            channel="slack",
            chat_id="C123",
            content="done",
            metadata={
                "slack": {"event": {"ts": "1700000000.000100"}, "channel_type": "channel"},
            },
        )
    )

    assert fake_web.reactions_remove_calls == [
        {"channel": "C123", "name": "eyes", "timestamp": "1700000000.000100"}
    ]
    assert fake_web.reactions_add_calls == [
        {"channel": "C123", "name": "white_check_mark", "timestamp": "1700000000.000100"}
    ]


@pytest.mark.asyncio
async def test_send_resolves_channel_name_to_channel_id() -> None:
    channel = SlackChannel(SlackConfig(enabled=True), MessageBus())
    fake_web = _FakeAsyncWebClient()
    fake_web._conversations_pages = [
        {
            "channels": [{"id": "C999", "name": "channel_x"}],
            "response_metadata": {"next_cursor": ""},
        }
    ]
    channel._web_client = fake_web

    await channel.send(
        OutboundMessage(
            channel="slack",
            chat_id="#channel_x",
            content="hello",
        )
    )

    assert fake_web.chat_post_calls == [
        {"channel": "C999", "text": "hello", "thread_ts": None}
    ]
    assert len(fake_web.conversations_list_calls) == 1


@pytest.mark.asyncio
async def test_send_resolves_user_handle_to_dm_channel() -> None:
    channel = SlackChannel(SlackConfig(enabled=True), MessageBus())
    fake_web = _FakeAsyncWebClient()
    fake_web._users_pages = [
        {
            "members": [
                {
                    "id": "U234",
                    "name": "alice",
                    "profile": {"display_name": "Alice"},
                }
            ],
            "response_metadata": {"next_cursor": ""},
        }
    ]
    fake_web._open_dm_response = {"channel": {"id": "D234"}}
    channel._web_client = fake_web

    await channel.send(
        OutboundMessage(
            channel="slack",
            chat_id="@alice",
            content="hello",
        )
    )

    assert fake_web.conversations_open_calls == [{"users": "U234"}]
    assert fake_web.chat_post_calls == [
        {"channel": "D234", "text": "hello", "thread_ts": None}
    ]


@pytest.mark.asyncio
async def test_send_updates_reaction_on_origin_channel_for_cross_channel_send() -> None:
    channel = SlackChannel(SlackConfig(enabled=True, react_emoji="eyes"), MessageBus())
    fake_web = _FakeAsyncWebClient()
    fake_web._conversations_pages = [
        {
            "channels": [{"id": "C999", "name": "channel_x"}],
            "response_metadata": {"next_cursor": ""},
        }
    ]
    channel._web_client = fake_web

    await channel.send(
        OutboundMessage(
            channel="slack",
            chat_id="channel_x",
            content="done",
            metadata={
                "slack": {
                    "event": {"ts": "1700000000.000100", "channel": "D_ORIGIN"},
                    "channel_type": "im",
                },
            },
        )
    )

    assert fake_web.chat_post_calls == [
        {"channel": "C999", "text": "done", "thread_ts": None}
    ]
    assert fake_web.reactions_remove_calls == [
        {"channel": "D_ORIGIN", "name": "eyes", "timestamp": "1700000000.000100"}
    ]
    assert fake_web.reactions_add_calls == [
        {"channel": "D_ORIGIN", "name": "white_check_mark", "timestamp": "1700000000.000100"}
    ]


@pytest.mark.asyncio
async def test_send_does_not_reuse_origin_thread_ts_for_cross_channel_send() -> None:
    channel = SlackChannel(SlackConfig(enabled=True), MessageBus())
    fake_web = _FakeAsyncWebClient()
    fake_web._conversations_pages = [
        {
            "channels": [{"id": "C999", "name": "channel_x"}],
            "response_metadata": {"next_cursor": ""},
        }
    ]
    channel._web_client = fake_web

    await channel.send(
        OutboundMessage(
            channel="slack",
            chat_id="channel_x",
            content="done",
            metadata={
                "slack": {
                    "event": {"ts": "1700000000.000100", "channel": "C_ORIGIN"},
                    "thread_ts": "1700000000.000200",
                    "channel_type": "channel",
                },
            },
        )
    )

    assert fake_web.chat_post_calls == [
        {"channel": "C999", "text": "done", "thread_ts": None}
    ]


@pytest.mark.asyncio
async def test_send_raises_when_named_target_cannot_be_resolved() -> None:
    channel = SlackChannel(SlackConfig(enabled=True), MessageBus())
    fake_web = _FakeAsyncWebClient()
    channel._web_client = fake_web

    with pytest.raises(ValueError, match="was not found"):
        await channel.send(
            OutboundMessage(
                channel="slack",
                chat_id="#missing-channel",
                content="hello",
            )
        )


@pytest.mark.asyncio
async def test_with_thread_context_fetches_root_once() -> None:
    channel = SlackChannel(SlackConfig(enabled=True), MessageBus())
    channel._bot_user_id = "UBOT"
    fake_web = _FakeAsyncWebClient()
    fake_web._conversations_replies_response = {
        "messages": [
            {"ts": "111.000", "user": "UROOT", "text": "drink water"},
            {"ts": "112.000", "user": "U2", "text": "good idea"},
            {"ts": "112.500", "user": "UBOT", "text": "I'll remind you."},
            {"ts": "113.000", "user": "U3", "text": "<@UBOT> what did you see?"},
        ]
    }
    channel._web_client = fake_web

    content = await channel._with_thread_context(
        "what did you see?",
        chat_id="C123",
        channel_type="channel",
        thread_ts="111.000",
        raw_thread_ts="111.000",
        current_ts="113.000",
    )

    assert fake_web.conversations_replies_calls == [
        {"channel": "C123", "ts": "111.000", "limit": 20}
    ]
    assert "Slack thread context before this mention:" in content
    assert "- <@UROOT>: drink water" in content
    assert "- <@U2>: good idea" in content
    assert "- bot: I'll remind you." in content
    assert "U3" not in content
    assert content.endswith("Current message:\nwhat did you see?")

    second = await channel._with_thread_context(
        "again",
        chat_id="C123",
        channel_type="channel",
        thread_ts="111.000",
        raw_thread_ts="111.000",
        current_ts="114.000",
    )
    assert second == "again"
    assert len(fake_web.conversations_replies_calls) == 1


@pytest.mark.asyncio
async def test_with_thread_context_fetches_replies_in_dm_thread() -> None:
    """DM threads should also pull thread history (not only channel threads)."""
    channel = SlackChannel(SlackConfig(enabled=True), MessageBus())
    channel._bot_user_id = "UBOT"
    fake_web = _FakeAsyncWebClient()
    fake_web._conversations_replies_response = {
        "messages": [
            {"ts": "211.000", "user": "UA", "text": "here is the file"},
            {"ts": "212.000", "user": "UA", "text": "please read it"},
        ]
    }
    channel._web_client = fake_web

    content = await channel._with_thread_context(
        "what did you see?",
        chat_id="D123",
        channel_type="im",
        thread_ts="211.000",
        raw_thread_ts="211.000",
        current_ts="213.000",
    )

    assert fake_web.conversations_replies_calls == [
        {"channel": "D123", "ts": "211.000", "limit": 20}
    ]
    assert "Slack thread context before this mention:" in content
    assert "- <@UA>: here is the file" in content


@pytest.mark.asyncio
async def test_dm_root_message_has_no_thread_ts_and_no_thread_session() -> None:
    """A top-level DM should not synthesize a thread_ts and uses the default session."""
    channel = SlackChannel(SlackConfig(enabled=True), MessageBus())
    channel._bot_user_id = "UBOT"
    channel._web_client = _FakeAsyncWebClient()
    channel._handle_message = AsyncMock()  # type: ignore[method-assign]
    client = SimpleNamespace(send_socket_mode_response=AsyncMock())
    req = SimpleNamespace(
        type="events_api",
        envelope_id="env-dm-root",
        payload={
            "event": {
                "type": "message",
                "user": "U1",
                "channel": "D123",
                "channel_type": "im",
                "text": "hello",
                "ts": "1700000000.000100",
            }
        },
    )

    await channel._on_socket_request(client, req)

    channel._handle_message.assert_awaited_once()
    kwargs = channel._handle_message.await_args.kwargs
    assert kwargs["session_key"] is None
    assert kwargs["metadata"]["slack"]["thread_ts"] is None


@pytest.mark.asyncio
async def test_dm_thread_message_keeps_thread_ts_and_threaded_session() -> None:
    """A DM message inside a real thread should preserve thread_ts and isolate the session."""
    channel = SlackChannel(SlackConfig(enabled=True), MessageBus())
    channel._bot_user_id = "UBOT"
    channel._web_client = _FakeAsyncWebClient()
    channel._handle_message = AsyncMock()  # type: ignore[method-assign]
    channel._with_thread_context = AsyncMock(return_value="hello")  # type: ignore[method-assign]
    client = SimpleNamespace(send_socket_mode_response=AsyncMock())
    req = SimpleNamespace(
        type="events_api",
        envelope_id="env-dm-thread",
        payload={
            "event": {
                "type": "message",
                "user": "U1",
                "channel": "D123",
                "channel_type": "im",
                "text": "hello",
                "ts": "1700000000.000200",
                "thread_ts": "1700000000.000100",
            }
        },
    )

    await channel._on_socket_request(client, req)

    channel._handle_message.assert_awaited_once()
    kwargs = channel._handle_message.await_args.kwargs
    assert kwargs["session_key"] == "slack:D123:1700000000.000100"
    assert kwargs["metadata"]["slack"]["thread_ts"] == "1700000000.000100"


def _channel_mention_req(text: str, ts: str = "111.000", thread_ts: str | None = None):
    event: dict[str, object] = {
        "type": "app_mention",
        "user": "U1",
        "channel": "C123",
        "channel_type": "channel",
        "text": text,
        "ts": ts,
    }
    if thread_ts is not None:
        event["thread_ts"] = thread_ts
    return SimpleNamespace(type="events_api", envelope_id="env-x", payload={"event": event})


async def _capture_inbound(req) -> dict:
    channel = SlackChannel(SlackConfig(enabled=True), MessageBus())
    channel._bot_user_id = "UBOT"
    channel._web_client = _FakeAsyncWebClient()
    channel._handle_message = AsyncMock()  # type: ignore[method-assign]
    channel._with_thread_context = AsyncMock(return_value="body")  # type: ignore[method-assign]
    client = SimpleNamespace(send_socket_mode_response=AsyncMock())
    await channel._on_socket_request(client, req)
    channel._handle_message.assert_awaited_once()
    return channel._handle_message.await_args.kwargs["metadata"]["slack"]


@pytest.mark.asyncio
async def test_channel_reply_goes_to_channel_by_default() -> None:
    """A plain @mention replies in the channel (reply_thread_ts=None), while the
    memory thread_ts is still set so project/memory keying is unaffected."""
    sl = await _capture_inbound(_channel_mention_req("<@UBOT> what's up"))
    assert sl["thread_ts"] == "111.000"      # memory key preserved
    assert sl["reply_thread_ts"] is None      # reply lands in the channel


@pytest.mark.asyncio
async def test_channel_reply_threads_when_brainstorm_present() -> None:
    """A /brainstorm mention opts into a threaded reply."""
    sl = await _capture_inbound(_channel_mention_req("<@UBOT> /brainstorm name ideas"))
    assert sl["reply_thread_ts"] == "111.000"


@pytest.mark.asyncio
async def test_brainstorm_trigger_is_case_insensitive() -> None:
    sl = await _capture_inbound(_channel_mention_req("<@UBOT> /BrainStorm please"))
    assert sl["reply_thread_ts"] == "111.000"


@pytest.mark.asyncio
async def test_existing_thread_message_keeps_threaded_reply() -> None:
    """A message already inside a thread keeps replying in that thread."""
    sl = await _capture_inbound(
        _channel_mention_req("<@UBOT> follow up", ts="222.000", thread_ts="100.000")
    )
    assert sl["reply_thread_ts"] == "100.000"


@pytest.mark.asyncio
async def test_send_replies_in_channel_when_reply_thread_ts_none() -> None:
    channel = SlackChannel(SlackConfig(enabled=True), MessageBus())
    fake_web = _FakeAsyncWebClient()
    channel._web_client = fake_web
    await channel.send(
        OutboundMessage(
            channel="slack",
            chat_id="C123",
            content="hi",
            metadata={
                "slack": {
                    "thread_ts": "111.000",
                    "reply_thread_ts": None,
                    "channel_type": "channel",
                }
            },
        )
    )
    assert fake_web.chat_post_calls[0]["thread_ts"] is None


@pytest.mark.asyncio
async def test_slack_slash_command_skips_thread_context() -> None:
    channel = SlackChannel(SlackConfig(enabled=True, allow_from=[]), MessageBus())
    channel._bot_user_id = "UBOT"
    channel._with_thread_context = AsyncMock(return_value="wrapped")  # type: ignore[method-assign]
    channel._handle_message = AsyncMock()  # type: ignore[method-assign]
    client = SimpleNamespace(send_socket_mode_response=AsyncMock())
    req = SimpleNamespace(
        type="events_api",
        envelope_id="env-1",
        payload={
            "event": {
                "type": "app_mention",
                "user": "U1",
                "channel": "C123",
                "text": "<@UBOT> /restart",
                "thread_ts": "111.000",
                "ts": "112.000",
            }
        },
    )

    await channel._on_socket_request(client, req)

    channel._with_thread_context.assert_not_awaited()
    channel._handle_message.assert_awaited_once()
    assert channel._handle_message.await_args.kwargs["content"] == "/restart"


@pytest.mark.asyncio
async def test_slack_file_share_downloads_media_and_reaches_agent() -> None:
    channel = SlackChannel(SlackConfig(enabled=True, bot_token="xoxb-test"), MessageBus())
    channel._bot_user_id = "UBOT"
    channel._web_client = _FakeAsyncWebClient()
    channel._handle_message = AsyncMock()  # type: ignore[method-assign]
    channel._download_slack_file = AsyncMock(  # type: ignore[method-assign]
        return_value=("/tmp/report.pdf", "[file: report.pdf]")
    )
    client = SimpleNamespace(send_socket_mode_response=AsyncMock())
    req = SimpleNamespace(
        type="events_api",
        envelope_id="env-file",
        payload={
            "event": {
                "type": "message",
                "subtype": "file_share",
                "user": "U1",
                "channel": "D123",
                "channel_type": "im",
                "text": "please read this",
                "ts": "1700000000.000100",
                "files": [
                    {
                        "id": "F123",
                        "name": "report.pdf",
                        "mimetype": "application/pdf",
                        "url_private_download": "https://files.slack.com/report.pdf",
                    }
                ],
            }
        },
    )

    await channel._on_socket_request(client, req)

    channel._download_slack_file.assert_awaited_once()
    channel._handle_message.assert_awaited_once()
    kwargs = channel._handle_message.await_args.kwargs
    assert kwargs["content"] == "please read this\n[file: report.pdf]"
    assert kwargs["media"] == ["/tmp/report.pdf"]


def test_slack_download_rejects_login_html() -> None:
    html_response = httpx.Response(
        200,
        headers={"content-type": "text/html; charset=utf-8"},
        content=b"<!doctype html><html><title>Sign in to Slack</title>",
    )
    markdown_response = httpx.Response(
        200,
        headers={"content-type": "text/markdown"},
        content=b"# PR Extraction Guide\n",
    )

    assert SlackChannel._looks_like_html_download(html_response) is True
    assert SlackChannel._looks_like_html_download(markdown_response) is False


def test_slack_download_failure_marker_is_actionable() -> None:
    marker = SlackChannel._download_failure_marker("image", "screenshot.png", "download failed")

    assert "not available to nanobot" in marker
    assert "files:read" in marker
    assert "reinstall the Slack app" in marker


def test_slack_channel_uses_channel_aware_allow_policy() -> None:
    channel = SlackChannel(SlackConfig(enabled=True, allow_from=[]), MessageBus())
    assert channel.is_allowed("U1") is True
    assert channel._is_allowed("U1", "C123", "channel") is True


# --- projectclaw: per-channel project resolution into metadata ---


def _project_map_cfg(**overrides):
    base = {
        "enabled": True,
        "project_map": {
            "C0123ABCDE": {
                "name": "foo",
                "github": {"repos": ["acme/foo-api"]},
            }
        },
    }
    base.update(overrides)
    return SlackConfig.model_validate(base)


async def _drive_app_mention(channel: SlackChannel, channel_id: str) -> dict:
    """Run one app_mention through _on_socket_request and return _handle_message kwargs."""
    channel._bot_user_id = "UBOT"
    channel._web_client = _FakeAsyncWebClient()
    channel._with_thread_context = AsyncMock(return_value="hello")  # type: ignore[method-assign]
    channel._handle_message = AsyncMock()  # type: ignore[method-assign]
    client = SimpleNamespace(send_socket_mode_response=AsyncMock())
    req = SimpleNamespace(
        type="events_api",
        envelope_id=f"env-project-{channel_id}",
        payload={
            "event": {
                "type": "app_mention",
                "user": "U1",
                "channel": channel_id,
                "text": "<@UBOT> status?",
                "ts": "1700000000.000100",
            }
        },
    )
    await channel._on_socket_request(client, req)
    channel._handle_message.assert_awaited_once()
    return channel._handle_message.await_args.kwargs


@pytest.mark.asyncio
async def test_inbound_from_mapped_channel_attaches_project() -> None:
    channel = SlackChannel(_project_map_cfg(), MessageBus())
    kwargs = await _drive_app_mention(channel, "C0123ABCDE")
    project = kwargs["metadata"].get("project")
    assert project is not None
    assert project["name"] == "foo"
    assert project["github"]["repos"] == ["acme/foo-api"]


@pytest.mark.asyncio
async def test_inbound_from_unmapped_channel_no_default_has_null_project() -> None:
    channel = SlackChannel(_project_map_cfg(), MessageBus())
    kwargs = await _drive_app_mention(channel, "C9999ZZZZZ")
    assert kwargs["metadata"].get("project") is None


@pytest.mark.asyncio
async def test_inbound_from_unmapped_channel_with_default_uses_default() -> None:
    channel = SlackChannel(_project_map_cfg(default_project="foo"), MessageBus())
    kwargs = await _drive_app_mention(channel, "C9999ZZZZZ")
    project = kwargs["metadata"].get("project")
    assert project is not None
    assert project["name"] == "foo"


# --- "thinking…" placeholder so the user sees the claw is working ---


def _thinking_channel(**overrides) -> tuple[SlackChannel, _FakeAsyncWebClient]:
    """A channel wired to a fake web client, with _handle_message stubbed."""
    channel = SlackChannel(SlackConfig(enabled=True, **overrides), MessageBus())
    channel._bot_user_id = "UBOT"
    fake = _FakeAsyncWebClient()
    channel._web_client = fake
    channel._with_thread_context = AsyncMock(return_value="body")  # type: ignore[method-assign]
    return channel, fake


@pytest.mark.asyncio
async def test_inbound_posts_thinking_placeholder() -> None:
    """An inbound message posts a 'thinking…' placeholder and registers its ts."""
    channel, fake = _thinking_channel()
    channel._handle_message = AsyncMock()  # type: ignore[method-assign]
    client = SimpleNamespace(send_socket_mode_response=AsyncMock())

    await channel._on_socket_request(client, _channel_mention_req("<@UBOT> hi"))

    placeholders = [c for c in fake.chat_post_calls if c["text"] == channel.config.thinking_text]
    assert len(placeholders) == 1
    assert placeholders[0]["channel"] == "C123"
    # default channel reply lands in-channel, so the placeholder is not threaded
    assert placeholders[0]["thread_ts"] is None
    # registered under "{chat_id}:{event_ts}" with the ts the post returned
    assert channel._thinking["C123:111.000"] == "ts.0001"


@pytest.mark.asyncio
async def test_send_replaces_thinking_placeholder_in_place() -> None:
    """The reply updates the placeholder in place rather than posting a new message."""
    channel, fake = _thinking_channel()
    channel._thinking["C123:1700000000.000100"] = "ph.1"

    await channel.send(
        OutboundMessage(
            channel="slack",
            chat_id="C123",
            content="the answer",
            metadata={
                "slack": {"event": {"ts": "1700000000.000100"}, "channel_type": "channel"},
            },
        )
    )

    assert fake.chat_update_calls == [{"channel": "C123", "ts": "ph.1", "text": "the answer"}]
    assert fake.chat_post_calls == []
    assert "C123:1700000000.000100" not in channel._thinking


@pytest.mark.asyncio
async def test_send_without_placeholder_posts_normally() -> None:
    """With no registered placeholder, send() posts a fresh message as before."""
    channel, fake = _thinking_channel()

    await channel.send(
        OutboundMessage(
            channel="slack",
            chat_id="C123",
            content="hi",
            metadata={
                "slack": {"event": {"ts": "1700000000.000100"}, "channel_type": "channel"},
            },
        )
    )

    assert fake.chat_update_calls == []
    assert [c["text"] for c in fake.chat_post_calls] == ["hi"]


@pytest.mark.asyncio
async def test_send_deletes_placeholder_when_reply_has_buttons() -> None:
    """A reply carrying buttons can't update in place, so the placeholder is deleted."""
    channel, fake = _thinking_channel()
    channel._thinking["C123:1700000000.000100"] = "ph.1"

    await channel.send(
        OutboundMessage(
            channel="slack",
            chat_id="C123",
            content="pick one",
            buttons=[["Yes", "yes"]],
            metadata={
                "slack": {"event": {"ts": "1700000000.000100"}, "channel_type": "channel"},
            },
        )
    )

    assert fake.chat_delete_calls == [{"channel": "C123", "ts": "ph.1"}]
    assert fake.chat_update_calls == []
    assert len(fake.chat_post_calls) == 1
    assert "blocks" in fake.chat_post_calls[0]
    assert "C123:1700000000.000100" not in channel._thinking


@pytest.mark.asyncio
async def test_thinking_placeholder_deleted_when_handle_raises() -> None:
    """If the turn never starts, the dangling placeholder is cleaned up."""
    channel, fake = _thinking_channel()
    channel._handle_message = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
    client = SimpleNamespace(send_socket_mode_response=AsyncMock())

    await channel._on_socket_request(client, _channel_mention_req("<@UBOT> hi"))

    assert "C123:111.000" not in channel._thinking
    assert fake.chat_delete_calls == [{"channel": "C123", "ts": "ts.0001"}]


@pytest.mark.asyncio
async def test_thinking_message_can_be_disabled() -> None:
    """thinking_message=False suppresses the placeholder entirely."""
    channel, fake = _thinking_channel(thinking_message=False)
    channel._handle_message = AsyncMock()  # type: ignore[method-assign]
    client = SimpleNamespace(send_socket_mode_response=AsyncMock())

    await channel._on_socket_request(client, _channel_mention_req("<@UBOT> hi"))

    assert all(c["text"] != channel.config.thinking_text for c in fake.chat_post_calls)
    assert channel._thinking == {}
