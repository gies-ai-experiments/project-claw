from nanobot.agent.tools.granola import GranolaToolConfig
from nanobot.meeting_classifier import service as svc
from nanobot.meeting_classifier.service import MeetingClassifierService


def _build(tmp_path, pages, calls, folder="fol_shared"):
    async def on_new(note):
        calls.append(note["id"])
    s = MeetingClassifierService(
        GranolaToolConfig(api_key="k"), folder, on_new,
        state_path=tmp_path / "state.json", interval_s=1,
        now_fn=lambda: "2026-07-17T00:00:00+00:00",
    )

    async def fake_get(cfg, path, params=None):
        return pages.pop(0)
    return s, fake_get


async def test_new_notes_fire_callback_once(tmp_path, monkeypatch):
    calls = []
    s, fake_get = _build(tmp_path, [{"notes": [{"id": "n1"}, {"id": "n2"}]}], calls)
    monkeypatch.setattr(svc, "_granola_get", fake_get)
    await s.tick()
    assert calls == ["n1", "n2"]


async def test_already_seen_not_reprocessed(tmp_path, monkeypatch):
    calls = []
    s, fake_get = _build(tmp_path, [
        {"notes": [{"id": "n1"}]},
        {"notes": [{"id": "n1"}, {"id": "n2"}]},
    ], calls)
    monkeypatch.setattr(svc, "_granola_get", fake_get)
    await s.tick()
    await s.tick()
    assert calls == ["n1", "n2"]


async def test_granola_error_fires_nothing(tmp_path, monkeypatch):
    calls = []
    s, fake_get = _build(tmp_path, ["Granola API error: boom"], calls)
    monkeypatch.setattr(svc, "_granola_get", fake_get)
    await s.tick()
    assert calls == []


async def test_no_folder_is_inert(tmp_path, monkeypatch):
    calls = []
    s, fake_get = _build(tmp_path, [{"notes": [{"id": "n1"}]}], calls, folder="")
    monkeypatch.setattr(svc, "_granola_get", fake_get)
    await s.tick()
    assert calls == []


async def test_state_persists_seen(tmp_path, monkeypatch):
    calls = []
    s, fake_get = _build(tmp_path, [{"notes": [{"id": "n1"}]}], calls)
    monkeypatch.setattr(svc, "_granola_get", fake_get)
    await s.tick()
    assert svc._load_state(tmp_path / "state.json")["seen"] == ["n1"]
