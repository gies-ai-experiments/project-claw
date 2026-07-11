"""Tests for the meeting-summary poller (deterministic detection + dedup)."""

from nanobot.agent.tools.granola import GranolaToolConfig
from nanobot.config.schema import (
    GranolaProjectConfig,
    MeetingSummaryProjectConfig,
    Project,
)
from nanobot.meeting_summary import service as svc
from nanobot.meeting_summary.service import MeetingSummaryService


def _project(name="P", folder="fld_1", enabled=True, channel="C1"):
    return Project(
        name=name,
        granola=GranolaProjectConfig(folder_id=folder),
        meeting_summary=MeetingSummaryProjectConfig(enabled=enabled, summary_channel=channel),
    )


def _build(tmp_path, projects, pages, calls):
    """Wire a service whose Granola client returns `pages` (popped per call)."""
    async def fake_get(cfg, path, params=None):
        return pages.pop(0)

    async def on_new(project, note):
        calls.append((project.name, note["id"]))

    s = MeetingSummaryService(
        GranolaToolConfig(api_key="k"), projects, on_new,
        state_path=tmp_path / "state.json", interval_s=1,
        now_fn=lambda: "2026-07-11T00:00:00+00:00",
    )
    return s, fake_get


async def test_new_notes_fire_callback_once(tmp_path, monkeypatch):
    calls = []
    s, fake_get = _build(tmp_path, [_project()], [{"notes": [{"id": "not_1"}, {"id": "not_2"}]}], calls)
    monkeypatch.setattr(svc, "_granola_get", fake_get)
    await s.tick()
    assert calls == [("P", "not_1"), ("P", "not_2")]


async def test_already_seen_not_reprocessed(tmp_path, monkeypatch):
    calls = []
    pages = [
        {"notes": [{"id": "not_1"}]},
        {"notes": [{"id": "not_1"}, {"id": "not_2"}]},  # not_1 already seen
    ]
    s, fake_get = _build(tmp_path, [_project()], pages, calls)
    monkeypatch.setattr(svc, "_granola_get", fake_get)
    await s.tick()
    await s.tick()
    assert calls == [("P", "not_1"), ("P", "not_2")]


async def test_opted_out_project_not_watched(tmp_path):
    s, _ = _build(tmp_path, [_project(enabled=False)], [], [])
    assert s.projects == []


async def test_missing_summary_channel_not_watched(tmp_path):
    s, _ = _build(tmp_path, [_project(channel="")], [], [])
    assert s.projects == []


async def test_granola_error_fires_nothing(tmp_path, monkeypatch):
    calls = []
    s, fake_get = _build(tmp_path, [_project()], ["Granola API error: boom"], calls)
    monkeypatch.setattr(svc, "_granola_get", fake_get)
    await s.tick()
    assert calls == []


async def test_state_persists_seen_ids(tmp_path, monkeypatch):
    calls = []
    s, fake_get = _build(tmp_path, [_project()], [{"notes": [{"id": "not_1"}]}], calls)
    monkeypatch.setattr(svc, "_granola_get", fake_get)
    await s.tick()
    reloaded = svc._load_state(tmp_path / "state.json")
    assert reloaded["fld_1"]["seen"] == ["not_1"]
