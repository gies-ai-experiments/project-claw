"""Exact-email identity joining across Slack and Asana."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from nanobot.meeting_classifier.models import PersonRef
from nanobot.meeting_classifier.repository import IdentityRecord, IdentityRepository


@dataclass(frozen=True)
class ResolvedIdentity:
    email: str
    display_name: str
    slack_user_id: str
    asana_user_gid: str
    verified_at: datetime


class IdentityValidationError(ValueError):
    def __init__(self, email: str, service: str, reason: str) -> None:
        self.email = email
        self.service = service
        self.reason = reason
        super().__init__(f"{email}: {service} identity {reason}")


class IdentityResolver:
    def __init__(
        self,
        repository: IdentityRepository,
        slack: Any,
        asana: Any,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._slack = slack
        self._asana = asana
        self._now = now or (lambda: datetime.now(UTC))

    async def resolve(self, person: PersonRef) -> ResolvedIdentity:
        email = person.email.strip().lower()
        if not email:
            raise IdentityValidationError(email, "email", "is blank")
        now = self._now()
        cached = await self._repository.get(email)
        if (
            cached is not None
            and cached.slack_user_id
            and cached.asana_user_gid
            and cached.verified_at is not None
            and cached.verified_at >= now - timedelta(hours=24)
        ):
            return ResolvedIdentity(
                email=email,
                display_name=cached.display_name,
                slack_user_id=cached.slack_user_id,
                asana_user_gid=cached.asana_user_gid,
                verified_at=cached.verified_at,
            )

        slack_user, asana_user = await asyncio.gather(
            self._resolve_service("Slack", self._slack.resolve_user_by_email, email),
            self._resolve_service("Asana", self._asana.resolve_user_by_email, email),
        )
        display_name = person.name.strip() or str(
            getattr(slack_user, "name", "") or getattr(asana_user, "name", "")
        )
        record = IdentityRecord(
            email=email,
            display_name=display_name,
            slack_user_id=str(slack_user.user_id),
            asana_user_gid=str(asana_user.gid),
            verified_at=now,
        )
        await self._repository.upsert_verified(record)
        return ResolvedIdentity(
            email=email,
            display_name=display_name,
            slack_user_id=record.slack_user_id or "",
            asana_user_gid=record.asana_user_gid or "",
            verified_at=now,
        )

    @staticmethod
    async def _resolve_service(service: str, method: Any, email: str) -> Any:
        result: Any = None
        failed = False
        try:
            result = await method(email)
        except Exception:
            failed = True
        if failed or result is None:
            raise IdentityValidationError(email, service, "could not be resolved")
        return result
