"""Sanitized Slack Web API boundary for project provisioning and meeting review."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from slack_sdk.errors import SlackApiError, SlackClientError
from slack_sdk.web.async_client import AsyncWebClient

from nanobot.integrations.errors import safe_external_error

_SAFE_FAILURE = safe_external_error("Slack", "request")
_SAFE_NOT_READY = "Slack client is not ready."
_SAFE_AMBIGUOUS = "Slack resource ownership is ambiguous."


@dataclass(frozen=True)
class SlackUser:
    user_id: str
    name: str
    email: str


@dataclass(frozen=True)
class SlackResource:
    channel_id: str
    name: str
    marker: str = ""
    timestamp: str = ""

    @property
    def resource_id(self) -> str:
        return self.channel_id


class SlackRetryableError(Exception):
    def __init__(
        self,
        safe_message: str = _SAFE_FAILURE,
        *,
        operation: str,
        code: str = "",
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(safe_message)
        self.safe_message = safe_message
        self.operation = operation
        self.code = code
        self.status_code = status_code
        self.retry_after = retry_after

    def __str__(self) -> str:
        return f"{self.safe_message} (operation={self.operation})"


class SlackPermanentError(Exception):
    def __init__(
        self,
        safe_message: str = _SAFE_FAILURE,
        *,
        operation: str,
        code: str = "",
        status_code: int | None = None,
    ) -> None:
        super().__init__(safe_message)
        self.safe_message = safe_message
        self.operation = operation
        self.code = code
        self.status_code = status_code

    def __str__(self) -> str:
        return f"{self.safe_message} (operation={self.operation})"


class SlackAmbiguousError(SlackPermanentError):
    def __init__(self, *, operation: str) -> None:
        super().__init__(_SAFE_AMBIGUOUS, operation=operation, code="ambiguous")


class SlackWorkspaceClient:
    """Small Web API adapter whose client becomes available after Socket Mode starts."""

    def __init__(self, client_provider: Callable[[], AsyncWebClient | None]) -> None:
        self._client_provider = client_provider

    def _client(self) -> AsyncWebClient:
        client: AsyncWebClient | None = None
        provider_failed = False
        try:
            client = self._client_provider()
        except Exception:
            provider_failed = True
        if provider_failed or client is None:
            raise SlackPermanentError(_SAFE_NOT_READY, operation="client")
        return client

    async def _call(
        self,
        operation: str,
        method: Callable[..., Awaitable[Any]],
        **kwargs: Any,
    ) -> Any:
        failure: SlackRetryableError | SlackPermanentError | None = None
        try:
            return await method(**kwargs)
        except SlackApiError as exc:
            response = exc.response
            status = getattr(response, "status_code", None)
            data = getattr(response, "data", None)
            if not isinstance(data, dict) and hasattr(response, "get"):
                data = {"error": response.get("error")}
            code = str((data or {}).get("error") or "")
            headers = getattr(response, "headers", None) or {}
            retry_after = _retry_after(headers.get("Retry-After"))
            if code == "ratelimited" or status == 429 or (
                isinstance(status, int) and 500 <= status < 600
            ):
                failure = SlackRetryableError(
                    operation=operation,
                    code=code,
                    status_code=status,
                    retry_after=retry_after,
                )
            else:
                failure = SlackPermanentError(
                    operation=operation,
                    code=code,
                    status_code=status,
                )
        except SlackClientError:
            failure = SlackRetryableError(operation=operation)
        if failure is None:  # pragma: no cover - defensive narrowing
            failure = SlackRetryableError(operation=operation)
        raise failure

    async def _active_users(self) -> list[dict[str, Any]]:
        client = self._client()
        cursor = ""
        users: list[dict[str, Any]] = []
        while True:
            response = await self._call(
                "users.list",
                client.users_list,
                limit=200,
                cursor=cursor,
            )
            for member in response.get("members") or []:
                if (
                    member.get("deleted")
                    or member.get("is_bot")
                    or member.get("is_app_user")
                ):
                    continue
                if member.get("id"):
                    users.append(member)
            cursor = str(
                ((response.get("response_metadata") or {}).get("next_cursor") or "")
            ).strip()
            if not cursor:
                return users

    async def resolve_user_by_email(self, email: str) -> SlackUser:
        normalized = _normalize_email(email)
        matches: list[SlackUser] = []
        for member in await self._active_users():
            profile = member.get("profile") or {}
            candidate = str(profile.get("email") or "").strip().lower()
            if candidate != normalized:
                continue
            matches.append(
                SlackUser(
                    user_id=str(member["id"]),
                    name=str(
                        profile.get("real_name")
                        or profile.get("display_name")
                        or member.get("name")
                        or ""
                    ),
                    email=candidate,
                )
            )
        if len(matches) > 1:
            raise SlackAmbiguousError(operation="users.list")
        if not matches:
            raise SlackPermanentError(
                "Slack workspace user was not found.", operation="users.list", code="not_found"
            )
        return matches[0]

    async def find_channel_by_slug(self, slug: str) -> SlackResource | None:
        normalized = _normalize_slug(slug)
        client = self._client()
        cursor = ""
        matches: list[SlackResource] = []
        while True:
            response = await self._call(
                "conversations.list",
                client.conversations_list,
                types="public_channel",
                exclude_archived=True,
                limit=200,
                cursor=cursor,
            )
            for channel in response.get("channels") or []:
                if str(channel.get("name") or "").lower() != normalized:
                    continue
                matches.append(
                    SlackResource(
                        channel_id=str(channel.get("id") or ""),
                        name=normalized,
                        marker=_channel_marker(channel),
                    )
                )
            cursor = str(
                ((response.get("response_metadata") or {}).get("next_cursor") or "")
            ).strip()
            if not cursor:
                break
        if len(matches) > 1:
            raise SlackAmbiguousError(operation="conversations.list")
        return matches[0] if matches else None

    async def create_public_channel(self, slug: str) -> SlackResource:
        normalized = _normalize_slug(slug)
        client = self._client()
        failure: SlackPermanentError | None = None
        try:
            response = await self._call(
                "conversations.create",
                client.conversations_create,
                name=normalized,
                is_private=False,
            )
        except SlackPermanentError as exc:
            failure = exc
            response = None
        if failure is not None:
            if failure.code != "name_taken":
                raise failure
            existing = await self.find_channel_by_slug(normalized)
            if existing is None or not existing.marker.startswith("projectclaw:"):
                raise SlackAmbiguousError(operation="conversations.create")
            return existing
        channel = (response or {}).get("channel") or {}
        channel_id = str(channel.get("id") or "")
        if not channel_id:
            raise SlackPermanentError(operation="conversations.create", code="invalid_response")
        return SlackResource(channel_id=channel_id, name=str(channel.get("name") or normalized))

    async def set_channel_marker(self, channel_id: str, marker: str) -> None:
        canonical = _canonical_marker(marker)
        client = self._client()
        await self._call(
            "conversations.setTopic",
            client.conversations_setTopic,
            channel=channel_id,
            topic=canonical,
        )

    async def invite_users(self, channel_id: str, user_ids: list[str]) -> None:
        requested = sorted({value.strip() for value in user_ids if value.strip()})
        if not requested:
            return
        existing = {str(member["id"]) for member in await self._active_users()}
        if any(user_id not in existing for user_id in requested):
            raise SlackPermanentError(
                "Slack workspace user was not found.",
                operation="conversations.invite",
                code="user_not_found",
            )
        client = self._client()
        await self._call(
            "conversations.invite",
            client.conversations_invite,
            channel=channel_id,
            users=",".join(requested),
        )

    async def open_dm(self, user_id: str) -> str:
        client = self._client()
        response = await self._call(
            "conversations.open", client.conversations_open, users=user_id
        )
        channel_id = str(((response.get("channel") or {}).get("id")) or "")
        if not channel_id:
            raise SlackPermanentError(operation="conversations.open", code="invalid_response")
        return channel_id

    async def post_blocks(self, channel_id: str, text: str, blocks: list[dict]) -> str:
        client = self._client()
        response = await self._call(
            "chat.postMessage",
            client.chat_postMessage,
            channel=channel_id,
            text=text,
            blocks=blocks,
        )
        ts = str(response.get("ts") or "")
        if not ts:
            raise SlackPermanentError(operation="chat.postMessage", code="invalid_response")
        return ts

    async def update_blocks(
        self, channel_id: str, ts: str, text: str, blocks: list[dict]
    ) -> None:
        client = self._client()
        await self._call(
            "chat.update",
            client.chat_update,
            channel=channel_id,
            ts=ts,
            text=text,
            blocks=blocks,
        )

    async def open_modal(self, trigger_id: str, view: dict) -> None:
        client = self._client()
        await self._call("views.open", client.views_open, trigger_id=trigger_id, view=view)

    async def find_message_by_marker(
        self, channel_id: str, marker: str
    ) -> SlackResource | None:
        canonical = _canonical_marker(marker)
        client = self._client()
        cursor = ""
        matches: list[SlackResource] = []
        while True:
            response = await self._call(
                "conversations.history",
                client.conversations_history,
                channel=channel_id,
                limit=200,
                cursor=cursor,
            )
            for message in response.get("messages") or []:
                lines = {line.strip() for line in str(message.get("text") or "").splitlines()}
                if canonical in lines:
                    matches.append(
                        SlackResource(
                            channel_id=channel_id,
                            name="message",
                            marker=canonical,
                            timestamp=str(message.get("ts") or ""),
                        )
                    )
            cursor = str(
                ((response.get("response_metadata") or {}).get("next_cursor") or "")
            ).strip()
            if not cursor:
                break
        if len(matches) > 1:
            raise SlackAmbiguousError(operation="conversations.history")
        return matches[0] if matches else None


def _normalize_email(email: str) -> str:
    normalized = email.strip().lower()
    if not normalized:
        raise SlackPermanentError("Slack email is required.", operation="users.list")
    return normalized


def _normalize_slug(slug: str) -> str:
    normalized = re.sub(r"[^a-z0-9_-]+", "-", slug.strip().lower())
    normalized = re.sub(r"[-_]+", "-", normalized).strip("-_")[:80].rstrip("-_")
    if not normalized:
        raise SlackPermanentError("Slack channel name is invalid.", operation="channel.slug")
    return normalized


def _canonical_marker(marker: str) -> str:
    normalized = marker.strip()
    if "\n" in normalized or "\r" in normalized or not normalized.startswith("projectclaw:"):
        raise SlackPermanentError("Slack channel marker is invalid.", operation="channel.marker")
    return normalized


def _channel_marker(channel: dict[str, Any]) -> str:
    for field in ("topic", "purpose"):
        value = str(((channel.get(field) or {}).get("value")) or "")
        for line in value.splitlines():
            normalized = line.strip()
            if normalized.startswith("projectclaw:"):
                return normalized
    return ""


def _retry_after(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None
