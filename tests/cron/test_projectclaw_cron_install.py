"""Tests for slack-app/install_cron.py.

Asserts the installer creates the two projectclaw jobs with the right
schedule, message, channel routing, and is idempotent.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from nanobot.cron.service import CronService

_REPO_ROOT = Path(__file__).resolve().parents[2]
_INSTALLER_PATH = _REPO_ROOT / "slack-app" / "install_cron.py"


@pytest.fixture(scope="module")
def installer():
    spec = importlib.util.spec_from_file_location(
        "_projectclaw_install_cron", _INSTALLER_PATH
    )
    assert spec and spec.loader, f"could not load installer at {_INSTALLER_PATH}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_projectclaw_install_cron"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path


def _jobs(workspace: Path) -> dict:
    """Return {name: job} dict from the store."""
    svc = CronService(workspace / "cron" / "jobs.json")
    return {j.name: j for j in svc.list_jobs(include_disabled=True)}


def test_install_creates_both_jobs(installer, workspace):
    outcomes = installer.install(workspace, verbose=False)
    assert outcomes == {
        "projectclaw-daily-standup": "added",
        "projectclaw-weekly-summary": "added",
    }
    jobs = _jobs(workspace)
    assert set(jobs.keys()) == {"projectclaw-daily-standup", "projectclaw-weekly-summary"}


def test_daily_standup_has_correct_shape(installer, workspace):
    installer.install(workspace, verbose=False)
    daily = _jobs(workspace)["projectclaw-daily-standup"]
    assert daily.enabled is True
    assert daily.schedule.kind == "cron"
    assert daily.schedule.expr == "0 9 * * 2-5"
    assert daily.payload.channel == "slack"
    assert daily.payload.to == "C0B6FAWLRA7"
    assert daily.payload.deliver is True
    assert "open PRs awaiting review" in daily.payload.message
    assert "merged in the last 24 hours" in daily.payload.message


def test_weekly_summary_has_correct_shape(installer, workspace):
    installer.install(workspace, verbose=False)
    weekly = _jobs(workspace)["projectclaw-weekly-summary"]
    assert weekly.enabled is True
    assert weekly.schedule.kind == "cron"
    assert weekly.schedule.expr == "0 9 * * 1"
    assert weekly.payload.channel == "slack"
    assert weekly.payload.to == "C0B6FAWLRA7"
    assert "merged in the last 7 days" in weekly.payload.message
    assert "aging" in weekly.payload.message
    assert "issues" in weekly.payload.message.lower()


def test_install_is_idempotent(installer, workspace):
    """Running twice must not create duplicate jobs."""
    installer.install(workspace, verbose=False)
    outcomes_second = installer.install(workspace, verbose=False)
    assert outcomes_second == {
        "projectclaw-daily-standup": "already exists",
        "projectclaw-weekly-summary": "already exists",
    }
    jobs = _jobs(workspace)
    assert len(jobs) == 2


def test_schedules_do_not_collide_on_monday(installer, workspace):
    """Daily fires Tue-Fri, weekly fires Mon — never the same day."""
    installer.install(workspace, verbose=False)
    jobs = _jobs(workspace)
    daily_dow = jobs["projectclaw-daily-standup"].schedule.expr.split()[-1]
    weekly_dow = jobs["projectclaw-weekly-summary"].schedule.expr.split()[-1]
    assert daily_dow == "2-5"
    assert weekly_dow == "1"
