"""Slack channel implementation using Socket Mode."""

import asyncio
import re
from pathlib import Path
from typing import Any

import httpx
from loguru import logger
from pydantic import Field, model_validator
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.socket_mode.websockets import SocketModeClient
from slack_sdk.web.async_client import AsyncWebClient
from slackify_markdown import slackify_markdown

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.paths import get_media_dir
from nanobot.config.schema import Base, Project
from nanobot.pairing import is_approved
from nanobot.utils.helpers import safe_filename, split_message


class SlackDMConfig(Base):
    """Slack DM policy configuration."""

    enabled: bool = True
    policy: str = "open"
    allow_from: list[str] = Field(default_factory=list)


class ProjectChannel(Base):
    """Which projects a single Slack channel may host.

    Replaces the one-project-per-channel `project_map`: a channel can list several
    `allowed_projects`, with an optional `default_project` used when a turn does not
    name one explicitly.
    """

    allowed_projects: list[str] = Field(default_factory=list)
    default_project: str | None = None


_PROJECT_MAP_DEPRECATION_WARNED = False

# Config-path project resolution (memory-off): an explicit "[project]" prefix and
# "owner/name" repo slugs in the message body, mirroring the Postgres resolver.
_PROJECT_PREFIX_RE = re.compile(r"\[([a-zA-Z0-9_\-]+)\]")
_PROJECT_REPO_RE = re.compile(r"([a-zA-Z0-9_\-]+/[a-zA-Z0-9_\-\.]+)")


class SlackConfig(Base):
    """Slack channel configuration."""

    enabled: bool = False
    mode: str = "socket"
    webhook_path: str = "/slack/events"
    bot_token: str = ""
    app_token: str = ""
    user_token_read_only: bool = True
    reply_in_thread: bool = True
    react_emoji: str = "eyes"
    done_emoji: str = "white_check_mark"
    # Post a transient "thinking…" message while the agent works, so the user
    # knows a reply is coming; it is replaced in place by the answer.
    thinking_message: bool = True
    thinking_text: str = "🐾 the claw is thinking…"
    include_thread_context: bool = True
    thread_context_limit: int = 20
    allow_from: list[str] = Field(default_factory=list)
    group_policy: str = "mention"
    group_allow_from: list[str] = Field(default_factory=list)
    dm: SlackDMConfig = Field(default_factory=SlackDMConfig)
    # Legacy: one project per channel. Kept for back-compat; superseded by
    # `projects` + `project_channels` below (a channel can host many projects).
    project_map: dict[str, Project] = Field(default_factory=dict)
    default_project: str | None = None

    # New multi-project registry (channel-local).
    projects: dict[str, Project] = Field(default_factory=dict)
    project_channels: dict[str, ProjectChannel] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_project_map(self) -> "SlackConfig":
        for key in self.project_map:
            if not key or key[0] not in "CDGUW" or not key[1:].replace("_", "").isalnum():
                raise ValueError(
                    f"project_map key '{key}' is not a Slack channel ID "
                    "(must start with C/D/G/U/W). Channel names are not allowed."
                )
        if self.default_project is not None:
            names = {p.name for p in self.project_map.values()}
            if self.default_project not in names:
                raise ValueError(
                    f"default_project '{self.default_project}' is not present in project_map"
                )
        return self

    @model_validator(mode="after")
    def _shim_legacy_project_map(self) -> "SlackConfig":
        """Project the legacy `project_map` into `projects` + `project_channels`.

        Additive only — the legacy fields are left intact so existing readers
        (`slack.py` resolution, older tests) keep working.
        """
        if not self.project_map:
            return self
        for channel_id, project in self.project_map.items():
            self.projects.setdefault(project.name, project)
            pc = self.project_channels.setdefault(channel_id, ProjectChannel())
            if project.name not in pc.allowed_projects:
                pc.allowed_projects.append(project.name)
            if self.default_project == project.name and pc.default_project is None:
                pc.default_project = project.name
        global _PROJECT_MAP_DEPRECATION_WARNED
        if not _PROJECT_MAP_DEPRECATION_WARNED:
            logger.warning(
                "slack.project_map is deprecated; migrate to slack.projects + "
                "slack.projectChannels (allowedProjects / defaultProject per channel)"
            )
            _PROJECT_MAP_DEPRECATION_WARNED = True
        return self


SLACK_MAX_MESSAGE_LEN = 39_000  # Slack API allows ~40k; leave margin
SLACK_DOWNLOAD_TIMEOUT = 30.0
# Abort Socket Mode WSS handshake after this many seconds. REST auth_test can still
# succeed while WSS blocks (firewall / region). slack-sdk does not apply HTTP(S)_PROXY
# to websockets.connect — see slack_sdk.socket_mode.websockets.SocketModeClient.connect.
SLACK_SOCKET_CONNECT_TIMEOUT_S = 45.0
_HTML_DOWNLOAD_PREFIXES = (b"<!doctype html", b"<html")


class SlackChannel(BaseChannel):
    """Slack channel using Socket Mode."""

    name = "slack"
    display_name = "Slack"
    _SLACK_ID_RE = re.compile(r"^[CDGUW][A-Z0-9]{2,}$")
    _SLACK_CHANNEL_REF_RE = re.compile(r"^<#([A-Z0-9]+)(?:\|[^>]+)?>$")
    _SLACK_USER_REF_RE = re.compile(r"^<@([A-Z0-9]+)(?:\|[^>]+)?>$")

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return SlackConfig().model_dump(by_alias=True)

    _THREAD_CONTEXT_CACHE_LIMIT = 10_000

    def __init__(self, config: Any, bus: MessageBus):
        if isinstance(config, dict):
            config = SlackConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: SlackConfig = config
        self._web_client: AsyncWebClient | None = None
        self._socket_client: SocketModeClient | None = None
        self._bot_user_id: str | None = None
        self._target_cache: dict[str, str] = {}
        self._thread_context_attempted: set[str] = set()
        # "{chat_id}:{event_ts}" -> ts of the "thinking…" placeholder message,
        # awaiting replacement (or cleanup) when the reply lands.
        self._thinking: dict[str, str] = {}
        # Optional handler for approval-style button clicks (value starts "mtg-"),
        # wired at boot. When set, such clicks route here instead of the agent loop.
        self._approval_callback: Any = None

    def set_approval_callback(self, cb: Any) -> None:
        """Route button clicks whose value starts 'mtg-' to `cb(sender_id, value)`."""
        self._approval_callback = cb

    async def start(self) -> None:
        """Start the Slack Socket Mode client."""
        if not self.config.bot_token or not self.config.app_token:
            self.logger.error("bot/app token not configured")
            return
        if self.config.mode != "socket":
            self.logger.error("Unsupported mode: {}", self.config.mode)
            return

        self._running = True

        self._web_client = AsyncWebClient(token=self.config.bot_token)
        self._socket_client = SocketModeClient(
            app_token=self.config.app_token,
            web_client=self._web_client,
        )

        self._socket_client.socket_mode_request_listeners.append(self._on_socket_request)

        # Resolve bot user ID for mention handling
        try:
            auth = await self._web_client.auth_test()
            self._bot_user_id = auth.get("user_id")
            self.logger.info("bot connected as {}", self._bot_user_id)
        except Exception as e:
            self.logger.warning("auth_test failed: {}", e)

        self.logger.info("Starting Socket Mode client...")
        try:
            await asyncio.wait_for(
                self._socket_client.connect(),
                timeout=SLACK_SOCKET_CONNECT_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            self.logger.error(
                "Slack Socket Mode WebSocket handshake timed out after {:.0f}s. "
                "auth_test uses HTTPS and may still succeed while WSS is blocked. "
                "Check outbound access to Slack WebSockets; slack-sdk Socket Mode "
                "does not apply HTTP(S)_PROXY to websockets.connect.",
                SLACK_SOCKET_CONNECT_TIMEOUT_S,
            )
            await self.stop()
            raise RuntimeError("Slack Socket Mode WebSocket connect timed out") from None

        self.logger.info("Slack Socket Mode WebSocket connected (events enabled)")

        while self._running:
            await asyncio.sleep(1)

    async def stop(self) -> None:
        """Stop the Slack client."""
        self._running = False
        if self._socket_client:
            try:
                await self._socket_client.close()
            except Exception as e:
                self.logger.warning("socket close failed: {}", e)
            self._socket_client = None

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through Slack."""
        if not self._web_client:
            self.logger.warning("client not running")
            return
        try:
            target_chat_id = await self._resolve_target_chat_id(msg.chat_id)
            slack_meta = msg.metadata.get("slack", {}) if msg.metadata else {}
            # Prefer the explicit reply target (channel vs thread) decided at
            # ingest; fall back to thread_ts for senders that don't set it
            # (cron, heartbeat, proactive message tool).
            thread_ts = slack_meta.get("reply_thread_ts", slack_meta.get("thread_ts"))
            origin_chat_id = str((slack_meta.get("event", {}) or {}).get("channel") or msg.chat_id)
            # Reply in the same thread the inbound message belongs to (works
            # for both real channel threads and DM threads). When the agent
            # is forwarding to a different channel, drop thread_ts because it
            # only makes sense within the originating conversation.
            thread_ts_param = thread_ts if thread_ts and target_chat_id == origin_chat_id else None

            is_progress = (msg.metadata or {}).get("_progress", False)
            event = slack_meta.get("event", {}) or {}
            origin_ts = event.get("ts")
            # The "thinking…" placeholder lives in the origin conversation, so only
            # a same-conversation reply can claim it.
            placeholder_key = (
                self._thinking_key(origin_chat_id, origin_ts)
                if origin_ts and target_chat_id == origin_chat_id
                else None
            )

            if is_progress and not msg.content:
                pass  # skip empty progress messages (e.g. tool-event-only updates)
            elif msg.content or not (msg.media or []):
                mrkdwn = self._to_mrkdwn(msg.content) if msg.content else " "
                buttons = getattr(msg, "buttons", None) or []
                chunks = split_message(mrkdwn, SLACK_MAX_MESSAGE_LEN)
                placeholder_ts = (
                    self._thinking.pop(placeholder_key, None) if placeholder_key else None
                )
                for index, chunk in enumerate(chunks):
                    kwargs: dict[str, Any] = dict(
                        channel=target_chat_id, text=chunk, thread_ts=thread_ts_param,
                    )
                    if buttons and index == len(chunks) - 1:
                        kwargs["blocks"] = self._build_button_blocks(chunk, buttons)
                    # Replace the "thinking…" placeholder in place with the first
                    # chunk. Buttons (blocks) can't be swapped cleanly, so fall back
                    # to deleting the placeholder and posting a fresh message.
                    if index == 0 and placeholder_ts and "blocks" not in kwargs:
                        await self._web_client.chat_update(
                            channel=target_chat_id, ts=placeholder_ts, text=chunk,
                        )
                        placeholder_ts = None
                        continue
                    if index == 0 and placeholder_ts:
                        await self._delete_thinking_message(target_chat_id, placeholder_ts)
                        placeholder_ts = None
                    await self._web_client.chat_postMessage(**kwargs)

            for media_path in msg.media or []:
                try:
                    await self._web_client.files_upload_v2(
                        channel=target_chat_id,
                        file=media_path,
                        thread_ts=thread_ts_param,
                    )
                except Exception:
                    self.logger.exception("Failed to upload file {}", media_path)

            # Update reaction emoji when the final (non-progress) response is sent
            if not is_progress:
                await self._update_react_emoji(origin_chat_id, event.get("ts"))
                # A final reply that never claimed the placeholder (e.g. media-only)
                # leaves it dangling — drop it so "thinking…" doesn't linger.
                if placeholder_key:
                    leftover = self._thinking.pop(placeholder_key, None)
                    if leftover:
                        await self._delete_thinking_message(origin_chat_id, leftover)

        except Exception:
            self.logger.exception("Error sending message")
            raise

    def _resolve_inbound_project(self, chat_id: str, text: str = "") -> dict[str, Any] | None:
        """Resolve the project for an inbound message from channel-local config.

        The memory-off counterpart to the Postgres ``ProjectResolver``: an explicit
        ``[project]`` prefix wins, else a project name / known repo slug mentioned
        in the body, else the channel's single allowed project, else its configured
        default. Reads the new ``projects`` + ``project_channels`` structure (the
        legacy ``project_map`` is shimmed into it at load).
        """
        cfg = self.config
        pc = cfg.project_channels.get(chat_id)
        allowed = [n for n in (pc.allowed_projects if pc else []) if n in cfg.projects]
        if not allowed:
            return None

        def dump(name: str) -> dict[str, Any]:
            return cfg.projects[name].model_dump()

        body = text or ""
        # 1. An explicit [project] prefix is authoritative.
        prefixes = {m.group(1) for m in _PROJECT_PREFIX_RE.finditer(body) if m.group(1) in allowed}
        if len(prefixes) == 1:
            return dump(next(iter(prefixes)))
        if len(prefixes) > 1:
            return None  # ambiguous — let the agent ask which project

        # 2. Loose signals: a project name, or a known owner/name repo slug.
        candidates: set[str] = set()
        lower = body.lower()
        for name in allowed:
            if name.lower() in lower:
                candidates.add(name)
        for m in _PROJECT_REPO_RE.finditer(body):
            repo = m.group(1)
            for name in allowed:
                gh = cfg.projects[name].github
                if gh and repo in (gh.repos or []):
                    candidates.add(name)
        if len(candidates) == 1:
            return dump(next(iter(candidates)))

        # 3. A channel with exactly one allowed project — unambiguous default.
        if len(allowed) == 1:
            return dump(allowed[0])

        # 4. The channel's configured default project.
        if pc and pc.default_project and pc.default_project in cfg.projects:
            return dump(pc.default_project)

        return None

    @staticmethod
    def _is_brainstorm(text: str) -> bool:
        """True when a message opts into a threaded reply via the /brainstorm trigger."""
        return "/brainstorm" in (text or "").lower()

    async def _resolve_target_chat_id(self, target: str) -> str:
        """Resolve human-friendly Slack targets to concrete IDs when needed."""
        if not self._web_client:
            return target

        target = target.strip()
        if not target:
            return target

        if match := self._SLACK_CHANNEL_REF_RE.fullmatch(target):
            return match.group(1)
        if match := self._SLACK_USER_REF_RE.fullmatch(target):
            return await self._open_dm_for_user(match.group(1))
        if self._SLACK_ID_RE.fullmatch(target):
            if target.startswith(("U", "W")):
                return await self._open_dm_for_user(target)
            return target

        if target.startswith("#"):
            return await self._resolve_channel_name(target[1:])
        if target.startswith("@"):
            return await self._resolve_user_handle(target[1:])

        try:
            return await self._resolve_channel_name(target)
        except ValueError:
            return await self._resolve_user_handle(target)

    async def _resolve_channel_name(self, name: str) -> str:
        normalized = self._normalize_target_name(name)
        if not normalized:
            raise ValueError("Slack target channel name is empty")

        cache_key = f"channel:{normalized}"
        if cache_key in self._target_cache:
            return self._target_cache[cache_key]

        cursor: str | None = None
        while True:
            response = await self._web_client.conversations_list(
                types="public_channel,private_channel",
                exclude_archived=True,
                limit=200,
                cursor=cursor,
            )
            for channel in response.get("channels", []):
                if self._normalize_target_name(str(channel.get("name") or "")) == normalized:
                    channel_id = str(channel.get("id") or "")
                    if channel_id:
                        self._target_cache[cache_key] = channel_id
                        return channel_id
            cursor = ((response.get("response_metadata") or {}).get("next_cursor") or "").strip()
            if not cursor:
                break

        raise ValueError(
            f"Slack channel '{name}' was not found. Use a joined channel name like "
            f"'#general' or a concrete channel ID."
        )

    async def _resolve_user_handle(self, handle: str) -> str:
        normalized = self._normalize_target_name(handle)
        if not normalized:
            raise ValueError("Slack target user handle is empty")

        cache_key = f"user:{normalized}"
        if cache_key in self._target_cache:
            return self._target_cache[cache_key]

        cursor: str | None = None
        while True:
            response = await self._web_client.users_list(limit=200, cursor=cursor)
            for member in response.get("members", []):
                if self._member_matches_handle(member, normalized):
                    user_id = str(member.get("id") or "")
                    if not user_id:
                        continue
                    dm_id = await self._open_dm_for_user(user_id)
                    self._target_cache[cache_key] = dm_id
                    return dm_id
            cursor = ((response.get("response_metadata") or {}).get("next_cursor") or "").strip()
            if not cursor:
                break

        raise ValueError(
            f"Slack user '{handle}' was not found. Use '@name' or a concrete DM/channel ID."
        )

    async def _open_dm_for_user(self, user_id: str) -> str:
        response = await self._web_client.conversations_open(users=user_id)
        channel_id = str(((response.get("channel") or {}).get("id")) or "")
        if not channel_id:
            raise ValueError(f"Slack DM target for user '{user_id}' could not be opened.")
        return channel_id

    @staticmethod
    def _normalize_target_name(value: str) -> str:
        return value.strip().lstrip("#@").lower()

    @classmethod
    def _member_matches_handle(cls, member: dict[str, Any], normalized: str) -> bool:
        profile = member.get("profile") or {}
        candidates = {
            str(member.get("name") or ""),
            str(profile.get("display_name") or ""),
            str(profile.get("display_name_normalized") or ""),
            str(profile.get("real_name") or ""),
            str(profile.get("real_name_normalized") or ""),
        }
        return normalized in {cls._normalize_target_name(candidate) for candidate in candidates if candidate}

    async def _on_socket_request(
        self,
        client: SocketModeClient,
        req: SocketModeRequest,
    ) -> None:
        """Handle incoming Socket Mode requests."""
        if req.type == "interactive":
            await self._on_block_action(client, req)
            return
        if req.type != "events_api":
            return

        # Acknowledge right away
        await client.send_socket_mode_response(
            SocketModeResponse(envelope_id=req.envelope_id)
        )

        payload = req.payload or {}
        event = payload.get("event") or {}
        event_type = event.get("type")

        # Handle app mentions or plain messages
        if event_type not in ("message", "app_mention"):
            return

        sender_id = event.get("user")
        chat_id = event.get("channel")

        subtype = event.get("subtype")
        # Slack uses subtype=file_share for user messages with attachments.
        # Ignore other subtypes such as bot_message / message_changed / deleted.
        if subtype and subtype != "file_share":
            return
        if self._bot_user_id and sender_id == self._bot_user_id:
            return

        # Avoid double-processing: Slack sends both `message` and `app_mention`
        # for mentions in channels. Prefer `app_mention`.
        text = event.get("text") or ""
        if event_type == "message" and self._bot_user_id and f"<@{self._bot_user_id}>" in text:
            return

        # Debug: log basic event shape
        self.logger.debug(
            "event: type={} subtype={} user={} channel={} channel_type={} text={}",
            event_type,
            subtype,
            sender_id,
            chat_id,
            event.get("channel_type"),
            text[:80],
        )
        if not sender_id or not chat_id:
            return

        channel_type = event.get("channel_type") or ""

        if not self._is_allowed(sender_id, chat_id, channel_type):
            if channel_type == "im" and self.config.dm.enabled:
                await self._handle_message(
                    sender_id=sender_id,
                    chat_id=chat_id,
                    content="",
                    is_dm=True,
                )
            return

        if channel_type != "im" and not self._should_respond_in_channel(event_type, text, chat_id):
            return

        text = self._strip_bot_mention(text)

        event_ts = event.get("ts")
        raw_thread_ts = event.get("thread_ts")
        thread_ts = raw_thread_ts
        # In DMs we don't auto-open a thread on top-level messages (it would
        # bury replies under "1 reply"). But if the user explicitly opened a
        # thread inside the DM, raw_thread_ts is set and we honor it.
        if (
            self.config.reply_in_thread
            and not thread_ts
            and channel_type != "im"
        ):
            thread_ts = event_ts
        # Replies land in the channel by default; thread only when the user opts
        # in with /brainstorm, or is already inside an existing thread. The
        # memory/session key keeps using thread_ts above, so project resolution
        # and conversation memory are unaffected by where the reply is posted.
        reply_thread_ts = thread_ts if (raw_thread_ts or self._is_brainstorm(text)) else None
        # Add :eyes: reaction to the triggering message (best-effort)
        try:
            if self._web_client and event.get("ts"):
                await self._web_client.reactions_add(
                    channel=chat_id,
                    name=self.config.react_emoji,
                    timestamp=event.get("ts"),
                )
        except Exception as e:
            self.logger.debug("reactions_add failed: {}", e)

        # Thread-scoped session key whenever the user is in a real thread
        # (raw_thread_ts is set). DM threads get their own session, separate
        # from the DM root, so context doesn't bleed across thread boundaries.
        session_key = (
            f"slack:{chat_id}:{thread_ts}" if thread_ts and raw_thread_ts else None
        )
        media_paths: list[str] = []
        file_markers: list[str] = []
        for file_info in event.get("files") or []:
            if not isinstance(file_info, dict):
                continue
            file_path, marker = await self._download_slack_file(file_info)
            if file_path:
                media_paths.append(file_path)
            if marker:
                file_markers.append(marker)

        is_slash = text.strip().startswith("/")
        content = text if is_slash else await self._with_thread_context(
            text,
            chat_id=chat_id,
            channel_type=channel_type,
            thread_ts=thread_ts,
            raw_thread_ts=raw_thread_ts,
            current_ts=event_ts,
        )
        if file_markers:
            content = "\n".join(part for part in [content, *file_markers] if part)
        if not content and not media_paths:
            return

        # Show a "thinking…" placeholder so the user knows a reply is coming.
        # It posts where the reply will land and is replaced in place by send().
        await self._post_thinking_placeholder(chat_id, event_ts, reply_thread_ts)

        try:
            await self._handle_message(
                sender_id=sender_id,
                chat_id=chat_id,
                content=content,
                media=media_paths,
                metadata={
                    "slack": {
                        "event": event,
                        "thread_ts": thread_ts,
                        "reply_thread_ts": reply_thread_ts,
                        "channel_type": channel_type,
                    },
                    "project": self._resolve_inbound_project(chat_id, content),
                },
                session_key=session_key,
            )
        except Exception:
            self.logger.exception("Error handling message from {}", sender_id)
            # The turn never started; drop the dangling "thinking…" placeholder.
            await self._clear_thinking_placeholder(chat_id, event_ts)

    async def _download_slack_file(self, file_info: dict[str, Any]) -> tuple[str | None, str]:
        """Download a Slack private file to the local media directory."""
        file_id = str(file_info.get("id") or "file")
        name = str(
            file_info.get("name")
            or file_info.get("title")
            or file_info.get("id")
            or "slack-file"
        )
        marker_type = "image" if str(file_info.get("mimetype") or "").startswith("image/") else "file"
        marker = f"[{marker_type}: {name}]"
        url = str(file_info.get("url_private_download") or file_info.get("url_private") or "")
        if not url:
            return None, self._download_failure_marker(marker_type, name, "missing download url")
        if not self.config.bot_token:
            return None, self._download_failure_marker(marker_type, name, "missing bot token")

        filename = safe_filename(f"{file_id}_{name}")
        path = Path(get_media_dir("slack")) / filename
        try:
            async with httpx.AsyncClient(timeout=SLACK_DOWNLOAD_TIMEOUT, follow_redirects=True) as client:
                response = await client.get(
                    url,
                    headers={"Authorization": f"Bearer {self.config.bot_token}"},
                )
                response.raise_for_status()
            if self._looks_like_html_download(response):
                raise ValueError("Slack returned HTML instead of file content")
            path.write_bytes(response.content)
            return str(path), marker
        except Exception as e:
            self.logger.warning("Failed to download file {}: {}", file_id, e)
            return None, self._download_failure_marker(marker_type, name, "download failed")

    @staticmethod
    def _download_failure_marker(marker_type: str, name: str, reason: str) -> str:
        return (
            f"[{marker_type}: {name}: {reason}; not available to nanobot. "
            "Check Slack files:read scope, reinstall the Slack app, and ensure the bot can access the file.]"
        )

    @staticmethod
    def _looks_like_html_download(response: httpx.Response) -> bool:
        content_type = response.headers.get("content-type", "").lower()
        if "text/html" in content_type:
            return True
        preview = response.content[:256].lstrip().lower()
        return preview.startswith(_HTML_DOWNLOAD_PREFIXES)

    async def _on_block_action(self, client: SocketModeClient, req: SocketModeRequest) -> None:
        """Handle button clicks from inline action buttons."""
        await client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        payload = req.payload or {}
        actions = payload.get("actions") or []
        if not actions:
            return
        value = str(actions[0].get("value") or "")
        user_info = payload.get("user") or {}
        sender_id = str(user_info.get("id") or "")
        channel_info = payload.get("channel") or {}
        chat_id = str(channel_info.get("id") or "")
        if not sender_id or not chat_id or not value:
            return
        # Approval buttons (meeting classifier) route to their own handler, not
        # the agent loop. The handler enforces its own admin check.
        if self._approval_callback is not None and value.startswith("mtg-"):
            try:
                await self._approval_callback(sender_id, value)
            except Exception:
                self.logger.exception("approval callback failed")
            return
        message_info = payload.get("message") or {}
        thread_ts = message_info.get("thread_ts") or message_info.get("ts")
        channel_type = self._infer_channel_type(chat_id)
        if not self._is_allowed(sender_id, chat_id, channel_type):
            return
        session_key = f"slack:{chat_id}:{thread_ts}" if thread_ts else None
        try:
            await self._handle_message(
                sender_id=sender_id,
                chat_id=chat_id,
                content=value,
                metadata={
                    "slack": {"thread_ts": thread_ts, "channel_type": channel_type},
                    "project": self._resolve_inbound_project(chat_id, value),
                },
                session_key=session_key,
            )
        except Exception:
            self.logger.exception("Error handling button click from {}", sender_id)

    async def _with_thread_context(
        self,
        text: str,
        *,
        chat_id: str,
        channel_type: str,
        thread_ts: str | None,
        raw_thread_ts: str | None,
        current_ts: str | None,
    ) -> str:
        """Include thread history the first time the bot is pulled into a Slack thread."""
        del channel_type  # DM and channel threads are both fetched via conversations.replies
        if (
            not self.config.include_thread_context
            or not self._web_client
            or not raw_thread_ts
            or not thread_ts
            or current_ts == thread_ts
        ):
            return text

        key = f"{chat_id}:{thread_ts}"
        if key in self._thread_context_attempted:
            return text
        if len(self._thread_context_attempted) >= self._THREAD_CONTEXT_CACHE_LIMIT:
            self._thread_context_attempted.clear()
        self._thread_context_attempted.add(key)

        try:
            response = await self._web_client.conversations_replies(
                channel=chat_id,
                ts=thread_ts,
                limit=max(1, self.config.thread_context_limit),
            )
        except Exception as e:
            self.logger.warning("thread context unavailable for {}: {}", key, e)
            return text

        lines = self._format_thread_context(
            response.get("messages", []),
            current_ts=current_ts,
        )
        if not lines:
            return text
        return "Slack thread context before this mention:\n" + "\n".join(lines) + f"\n\nCurrent message:\n{text}"

    def _format_thread_context(self, messages: list[dict[str, Any]], *, current_ts: str | None) -> list[str]:
        lines: list[str] = []
        for item in messages:
            if item.get("ts") == current_ts:
                continue
            if item.get("subtype"):
                continue
            sender = str(item.get("user") or item.get("bot_id") or "unknown")
            is_bot = self._bot_user_id is not None and sender == self._bot_user_id
            label = "bot" if is_bot else f"<@{sender}>"
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            text = self._strip_bot_mention(text)
            if len(text) > 500:
                text = text[:500] + "…"
            lines.append(f"- {label}: {text}")
        return lines

    @staticmethod
    def _build_button_blocks(text: str, buttons: list[list[Any]]) -> list[dict[str, Any]]:
        """Build Slack Block Kit blocks with action buttons.

        Each button item is either a plain ``str`` (label doubles as the click
        value, the legacy form) or a ``[label, value]`` pair so a button can show
        a friendly label while carrying a distinct action value. ``action_id`` is
        made unique per button so repeated labels (e.g. two "Approve"s) don't
        collide.
        """
        blocks: list[dict[str, Any]] = [
            {"type": "section", "text": {"type": "mrkdwn", "text": text[:3000]}},
        ]
        elements = []
        for row in buttons:
            for item in row:
                if isinstance(item, (list, tuple)):
                    label, value = str(item[0]), str(item[1])
                else:
                    label = value = str(item)
                elements.append({
                    "type": "button",
                    "text": {"type": "plain_text", "text": label[:75]},
                    "value": value[:75],
                    "action_id": f"btn_{len(elements)}",
                })
        if elements:
            blocks.append({"type": "actions", "elements": elements[:25]})
        return blocks

    @staticmethod
    def _thinking_key(chat_id: str, event_ts: str) -> str:
        """Registry key correlating an inbound message to its placeholder."""
        return f"{chat_id}:{event_ts}"

    async def _post_thinking_placeholder(
        self, chat_id: str, event_ts: str | None, thread_ts: str | None
    ) -> None:
        """Post a transient "thinking…" message and remember it for replacement.

        Best-effort: a failure here must never break handling the turn.
        """
        if not self.config.thinking_message or not self._web_client or not event_ts:
            return
        try:
            resp = await self._web_client.chat_postMessage(
                channel=chat_id,
                text=self.config.thinking_text,
                thread_ts=thread_ts,
            )
            # chat_postMessage returns a SlackResponse (dict-like, not a dict),
            # so read "ts" via .get() rather than an isinstance(dict) check.
            ts = resp.get("ts") if resp is not None else None
            if ts:
                self._thinking[self._thinking_key(chat_id, event_ts)] = ts
        except Exception as e:
            self.logger.debug("thinking placeholder post failed: {}", e)

    async def _clear_thinking_placeholder(self, chat_id: str, event_ts: str | None) -> None:
        """Drop a pending placeholder when the reply will never arrive."""
        if not event_ts:
            return
        ts = self._thinking.pop(self._thinking_key(chat_id, event_ts), None)
        if ts:
            await self._delete_thinking_message(chat_id, ts)

    async def _delete_thinking_message(self, chat_id: str, ts: str) -> None:
        """Best-effort deletion of a placeholder message."""
        if not self._web_client:
            return
        try:
            await self._web_client.chat_delete(channel=chat_id, ts=ts)
        except Exception as e:
            self.logger.debug("thinking placeholder delete failed: {}", e)

    async def _update_react_emoji(self, chat_id: str, ts: str | None) -> None:
        """Remove the in-progress reaction and optionally add a done reaction."""
        if not self._web_client or not ts:
            return
        try:
            await self._web_client.reactions_remove(
                channel=chat_id,
                name=self.config.react_emoji,
                timestamp=ts,
            )
        except Exception as e:
            self.logger.debug("reactions_remove failed: {}", e)
        if self.config.done_emoji:
            try:
                await self._web_client.reactions_add(
                    channel=chat_id,
                    name=self.config.done_emoji,
                    timestamp=ts,
                )
            except Exception as e:
                self.logger.debug("done reaction failed: {}", e)

    def _is_allowed(self, sender_id: str, chat_id: str, channel_type: str) -> bool:
        if channel_type == "im":
            if not self.config.dm.enabled:
                return False
            if self.config.dm.policy == "allowlist":
                return sender_id in self.config.dm.allow_from or is_approved(self.name, sender_id)
            return True

        # Group / channel messages
        if self.config.group_policy == "allowlist":
            return chat_id in self.config.group_allow_from
        return True

    def _should_respond_in_channel(self, event_type: str, text: str, chat_id: str) -> bool:
        if self.config.group_policy == "open":
            return True
        if self.config.group_policy == "mention":
            if event_type == "app_mention":
                return True
            return self._bot_user_id is not None and f"<@{self._bot_user_id}>" in text
        if self.config.group_policy == "allowlist":
            return chat_id in self.config.group_allow_from
        return False

    def is_allowed(self, sender_id: str) -> bool:
        # Slack needs channel-aware policy checks, so _on_socket_request and
        # _on_block_action call _is_allowed before handing off to BaseChannel.
        return True

    @staticmethod
    def _infer_channel_type(chat_id: str) -> str:
        if chat_id.startswith("D"):
            return "im"
        if chat_id.startswith("G"):
            return "group"
        return "channel"

    def _strip_bot_mention(self, text: str) -> str:
        if not text or not self._bot_user_id:
            return text
        return re.sub(rf"<@{re.escape(self._bot_user_id)}>\s*", "", text).strip()

    _TABLE_RE = re.compile(r"(?m)^\|.*\|$(?:\n\|[\s:|-]*\|$)(?:\n\|.*\|$)*")
    _CODE_FENCE_RE = re.compile(r"```[\s\S]*?```")
    _INLINE_CODE_RE = re.compile(r"`[^`]+`")
    _LEFTOVER_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
    _LEFTOVER_HEADER_RE = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)
    _BARE_URL_RE = re.compile(r"(?<![|<])(https?://\S+)")

    @classmethod
    def _to_mrkdwn(cls, text: str) -> str:
        """Convert Markdown to Slack mrkdwn, including tables."""
        if not text:
            return ""
        text = cls._TABLE_RE.sub(cls._convert_table, text)
        return cls._fixup_mrkdwn(slackify_markdown(text)).rstrip("\n")

    @classmethod
    def _fixup_mrkdwn(cls, text: str) -> str:
        """Fix markdown artifacts that slackify_markdown misses."""
        code_blocks: list[str] = []

        def _save_code(m: re.Match) -> str:
            code_blocks.append(m.group(0))
            return f"\x00CB{len(code_blocks) - 1}\x00"

        text = cls._CODE_FENCE_RE.sub(_save_code, text)
        text = cls._INLINE_CODE_RE.sub(_save_code, text)
        text = cls._LEFTOVER_BOLD_RE.sub(r"*\1*", text)
        text = cls._LEFTOVER_HEADER_RE.sub(r"*\1*", text)
        text = cls._BARE_URL_RE.sub(lambda m: m.group(0).replace("&amp;", "&"), text)

        for i, block in enumerate(code_blocks):
            text = text.replace(f"\x00CB{i}\x00", block)
        return text

    @staticmethod
    def _convert_table(match: re.Match) -> str:
        """Convert a Markdown table to a Slack-readable list."""
        lines = [ln.strip() for ln in match.group(0).strip().splitlines() if ln.strip()]
        if len(lines) < 2:
            return match.group(0)
        headers = [h.strip() for h in lines[0].strip("|").split("|")]
        start = 2 if re.fullmatch(r"[|\s:\-]+", lines[1]) else 1
        rows: list[str] = []
        for line in lines[start:]:
            cells = [c.strip() for c in line.strip("|").split("|")]
            cells = (cells + [""] * len(headers))[: len(headers)]
            parts = [f"**{headers[i]}**: {cells[i]}" for i in range(len(headers)) if cells[i]]
            if parts:
                rows.append(" · ".join(parts))
        return "\n".join(rows)
