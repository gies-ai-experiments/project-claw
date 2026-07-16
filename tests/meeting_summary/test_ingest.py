from nanobot.config.schema import GranolaProjectConfig, Project
from nanobot.meeting_summary.ingest import build_ingest_args, ingest_note


def _project():
    return Project(name="claw", granola=GranolaProjectConfig(folder_id="fld_1"))


def test_build_ingest_args_maps_fields():
    note = {"id": "not_9", "title": "Sync", "summary": "Decided X", "transcript": "…talk…"}
    a = build_ingest_args(_project(), note, channel_id="C1")
    assert a.project_id == "claw"          # project_id IS the name
    assert a.channel_type == "granola"
    assert a.channel_id == "C1"
    assert a.thread_ts == "granola:not_9"
    assert a.slack_ts == "not_9"           # idempotency key
    assert a.role == "user"
    assert "Sync" in a.body and "Decided X" in a.body and "talk" in a.body


async def test_ingest_note_calls_append_once():
    calls = []

    class FakeStore:
        async def append(self, a):
            calls.append(a)
            return 1

    note = {"id": "not_9", "title": "Sync", "summary": "s", "transcript": "t"}
    rid = await ingest_note(FakeStore(), _project(), note, channel_id="C1")
    assert rid == 1
    assert len(calls) == 1 and calls[0].slack_ts == "not_9"


async def test_ingest_note_skips_when_no_id():
    class FakeStore:
        async def append(self, a):
            raise AssertionError("should not append")

    assert await ingest_note(FakeStore(), _project(), {"title": "x"}, channel_id="C1") is None
