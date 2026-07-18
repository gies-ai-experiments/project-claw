from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from nanobot.meeting_classifier.identity import IdentityResolver, IdentityValidationError
from nanobot.meeting_classifier.models import PersonRef
from nanobot.meeting_classifier.repository import IdentityRecord


class Repo:
    def __init__(self, record=None):
        self.record = record
        self.saved = None

    async def get(self, _email):
        return self.record

    async def upsert_verified(self, record):
        self.saved = record


@pytest.mark.asyncio
async def test_identity_uses_fresh_complete_cache() -> None:
    now = datetime(2026, 7, 18, tzinfo=UTC)
    repo = Repo(IdentityRecord("ash@example.edu", "Ash", "U1", "A1", now))
    slack = SimpleNamespace(resolve_user_by_email=None)
    asana = SimpleNamespace(resolve_user_by_email=None)
    result = await IdentityResolver(repo, slack, asana, now=lambda: now).resolve(
        PersonRef(name="Ash", email=" ASH@example.edu ")
    )
    assert (result.slack_user_id, result.asana_user_gid) == ("U1", "A1")


@pytest.mark.asyncio
async def test_identity_refreshes_stale_cache_and_persists_pair() -> None:
    now = datetime(2026, 7, 18, tzinfo=UTC)
    repo = Repo(IdentityRecord("ash@example.edu", "Old", "OLD", "OLD", now - timedelta(hours=25)))

    class Slack:
        async def resolve_user_by_email(self, _email):
            return SimpleNamespace(user_id="U1", name="Ash")

    class Asana:
        async def resolve_user_by_email(self, _email):
            return SimpleNamespace(gid="A1", name="Ash")

    result = await IdentityResolver(repo, Slack(), Asana(), now=lambda: now).resolve(
        PersonRef(name="Ash", email="ash@example.edu")
    )
    assert (result.slack_user_id, result.asana_user_gid) == ("U1", "A1")
    assert repo.saved.verified_at == now


@pytest.mark.asyncio
async def test_identity_reports_service_without_leaking_raw_error() -> None:
    class MissingSlack:
        async def resolve_user_by_email(self, _email):
            raise RuntimeError("xoxb-secret")

    class Asana:
        async def resolve_user_by_email(self, _email):
            return SimpleNamespace(gid="A1", name="Ash")

    with pytest.raises(IdentityValidationError, match="Slack") as exc_info:
        await IdentityResolver(Repo(), MissingSlack(), Asana()).resolve(
            PersonRef(name="Ash", email="ash@example.edu")
        )
    assert "xoxb-secret" not in str(exc_info.value)
    assert exc_info.value.__context__ is None
