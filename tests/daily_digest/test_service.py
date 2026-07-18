from nanobot.config.schema import (
    DailyDigestProjectConfig,
    GitHubProjectConfig,
    GranolaProjectConfig,
    MeetingSummaryProjectConfig,
    Project,
)
from nanobot.daily_digest import service as svc
from nanobot.daily_digest.service import DailyDigestService


def _project(name="P", channel="C1", digest_channel="", summary_channel="", github=True):
    kwargs = dict(name=name, channel=channel)
    if github:
        kwargs["github"] = GitHubProjectConfig(repos=["o/r"])
    else:
        kwargs["granola"] = GranolaProjectConfig(folder_id="f")  # a non-github source
    if digest_channel:
        kwargs["daily_digest"] = DailyDigestProjectConfig(enabled=True, digest_channel=digest_channel)
    if summary_channel:
        kwargs["meeting_summary"] = MeetingSummaryProjectConfig(enabled=True, summary_channel=summary_channel)
    return Project(**kwargs)


def _build(tmp_path, projects, calls, date="2026-07-18"):
    async def on_digest(project):
        calls.append((project.name, date))
    s = DailyDigestService(
        projects, on_digest, state_path=tmp_path / "dd.json", now_date_fn=lambda: date,
    )
    return s


async def test_fires_once_per_project(tmp_path):
    calls = []
    s = _build(tmp_path, [_project()], calls)
    await s.tick()
    assert calls == [("P", "2026-07-18")]


async def test_not_fired_twice_same_day(tmp_path):
    calls = []
    s = _build(tmp_path, [_project()], calls)
    await s.tick()
    await s.tick()
    assert calls == [("P", "2026-07-18")]


async def test_watched_by_channel_field_without_optin(tmp_path):
    # "every channel": a project with a channel + github repos is covered,
    # no per-project daily_digest opt-in required.
    s = _build(tmp_path, [_project(channel="C9")], [])
    assert [p.name for p in s.projects] == ["P"]


async def test_channel_precedence():
    # digest_channel > project.channel > meeting_summary.summary_channel
    assert svc._channel(_project(channel="Cfield", digest_channel="Cdd")) == "Cdd"
    assert svc._channel(_project(channel="Cfield", summary_channel="Cms")) == "Cfield"
    assert svc._channel(_project(channel="", summary_channel="Cms")) == "Cms"


async def test_no_channel_not_watched(tmp_path):
    s = _build(tmp_path, [_project(channel="")], [])
    assert s.projects == []


async def test_no_github_not_watched(tmp_path):
    # needs a repo source to summarize; a granola-only project is skipped
    s = _build(tmp_path, [_project(channel="C1", github=False)], [])
    assert s.projects == []


async def test_state_persists_last_run_date(tmp_path):
    s = _build(tmp_path, [_project()], [])
    await s.tick()
    assert svc._load_state(tmp_path / "dd.json")["P"] == "2026-07-18"
