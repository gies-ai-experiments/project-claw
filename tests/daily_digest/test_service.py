from nanobot.config.schema import (
    DailyDigestProjectConfig,
    GitHubProjectConfig,
    MeetingSummaryProjectConfig,
    Project,
)
from nanobot.daily_digest import service as svc
from nanobot.daily_digest.service import DailyDigestService


def _project(name="P", enabled=True, channel="C1", fallback=""):
    return Project(
        name=name,
        github=GitHubProjectConfig(repos=["o/r"]),
        daily_digest=DailyDigestProjectConfig(enabled=enabled, digest_channel=channel),
        meeting_summary=(
            MeetingSummaryProjectConfig(enabled=True, summary_channel=fallback)
            if fallback else None
        ),
    )


def _build(tmp_path, projects, calls, date="2026-07-16"):
    async def on_digest(project):
        calls.append((project.name, date))
    s = DailyDigestService(
        projects, on_digest, state_path=tmp_path / "dd.json",
        now_date_fn=lambda: date,
    )
    return s


async def test_fires_once_per_project(tmp_path):
    calls = []
    s = _build(tmp_path, [_project()], calls)
    await s.tick()
    assert calls == [("P", "2026-07-16")]


async def test_not_fired_twice_same_day(tmp_path):
    calls = []
    s = _build(tmp_path, [_project()], calls)
    await s.tick()
    await s.tick()
    assert calls == [("P", "2026-07-16")]


async def test_opted_out_not_watched(tmp_path):
    s = _build(tmp_path, [_project(enabled=False)], [])
    assert s.projects == []


async def test_channel_falls_back_to_summary_channel():
    assert svc._channel(_project(channel="", fallback="C9")) == "C9"


async def test_no_channel_not_watched(tmp_path):
    s = _build(tmp_path, [_project(channel="")], [])
    assert s.projects == []


async def test_state_persists_last_run_date(tmp_path):
    s = _build(tmp_path, [_project()], [])
    await s.tick()
    assert svc._load_state(tmp_path / "dd.json")["P"] == "2026-07-16"
