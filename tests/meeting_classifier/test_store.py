from nanobot.meeting_classifier.store import ApprovalStore


def test_add_and_get_draft(tmp_path):
    s = ApprovalStore(tmp_path / "s.json")
    s.add_draft("not_1", "atlas", {"summary": "did X", "actions": ["a"]})
    got = s.get_draft("not_1", "atlas")
    assert got["status"] == "pending"
    assert got["draft"]["summary"] == "did X"


def test_get_unknown_is_none(tmp_path):
    s = ApprovalStore(tmp_path / "s.json")
    assert s.get_draft("nope", "atlas") is None


def test_mark_is_idempotent(tmp_path):
    s = ApprovalStore(tmp_path / "s.json")
    s.add_draft("not_1", "atlas", {"summary": "x"})
    assert s.mark("not_1", "atlas", "approved") is True   # first transition
    assert s.mark("not_1", "atlas", "approved") is False  # already decided
    assert s.status("not_1", "atlas") == "approved"


def test_mark_unknown_returns_false(tmp_path):
    s = ApprovalStore(tmp_path / "s.json")
    assert s.mark("ghost", "atlas", "approved") is False


def test_state_persists_across_reload(tmp_path):
    p = tmp_path / "s.json"
    s = ApprovalStore(p)
    s.add_draft("not_1", "atlas", {"summary": "x"})
    s.mark("not_1", "atlas", "skipped")
    reloaded = ApprovalStore(p)
    assert reloaded.status("not_1", "atlas") == "skipped"
    assert reloaded.mark("not_1", "atlas", "approved") is False  # stays decided
