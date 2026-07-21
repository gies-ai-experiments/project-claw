"""Fake Granola source (tools.granola.fakeMeetings) drives the pipeline offline."""

from __future__ import annotations

import nanobot.agent.tools.granola as granola
from nanobot.agent.tools.granola import GranolaToolConfig, _granola_get


def _reset() -> None:
    granola._FAKE_NOTES = None


async def test_fake_meetings_emits_stable_batch_and_getnote():
    _reset()
    cfg = GranolaToolConfig(api_key="", fake_meetings=3)
    lst = await _granola_get(cfg, "/notes", {"folder_id": "fol_x"})
    assert isinstance(lst, dict) and len(lst["notes"]) == 3
    nid = lst["notes"][0]["id"]
    note = await _granola_get(cfg, f"/notes/{nid}")
    assert note["id"] == nid and note["transcript"]  # body available for classify
    # Same ids across calls so the poller's id-dedup processes each once.
    lst2 = await _granola_get(cfg, "/notes", {"folder_id": "fol_x"})
    assert [n["id"] for n in lst2["notes"]] == [n["id"] for n in lst["notes"]]


async def test_fake_off_uses_real_path():
    _reset()
    cfg = GranolaToolConfig(api_key="", fake_meetings=0)
    assert await _granola_get(cfg, "/notes") == "Granola API error: api_key is not configured"
