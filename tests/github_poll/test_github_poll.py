"""GitHub poll service: repo->channel map + watermark seed / new-commit / dedup."""

from __future__ import annotations

from nanobot.config.schema import Project
from nanobot.github_poll.service import GithubPollService, build_repo_channel_map


def _proj(name, repos, channel):
    return Project.model_validate(
        {"name": name, "github": {"repos": repos}, "channel": channel, "description": "x"}
    )


def test_build_repo_channel_map_skips_channelless():
    projects = {
        "gc": _proj("gc", ["o/GiesChat"], "C1"),
        "nk": _proj("nk", ["o/x"], ""),  # no channel -> skipped
    }
    m = build_repo_channel_map(projects)
    assert m["o/GiesChat"] == ("gc", "C1")
    assert "o/x" not in m


class _FakeSvc(GithubPollService):
    """Override the network fetch with a scripted commit list (newest-first)."""

    def __init__(self, commits_ref, **kw):
        super().__init__(**kw)
        self._commits_ref = commits_ref

    async def _list_commits(self, client, repo):
        return self._commits_ref[0].get(repo, [])


async def test_seed_then_new_then_dedup(tmp_path):
    calls = []

    async def on_new(project, channel, repo, subjects):
        calls.append((repo, subjects))

    ref = [
        {
            "o/r": [
                {"sha": "c2", "commit": {"message": "Fix bug"}},
                {"sha": "c1", "commit": {"message": "Add login"}},
            ]
        }
    ]
    svc = _FakeSvc(
        ref,
        repo_channels={"o/r": ("p", "C1")},
        token="t",
        on_new=on_new,
        state_path=tmp_path / "s.json",
        interval_s=1,
    )

    await svc.tick()  # first tick seeds watermark, no post
    assert calls == []
    assert svc._state["o/r"] == "c2"

    ref[0]["o/r"] = [{"sha": "c3", "commit": {"message": "Add logout\n\nbody"}}] + ref[0]["o/r"]
    await svc.tick()  # only the new commit (c3) is posted, subject only
    assert calls == [("o/r", ["Add logout"])]
    assert svc._state["o/r"] == "c3"

    calls.clear()
    await svc.tick()  # nothing new -> no post
    assert calls == []
