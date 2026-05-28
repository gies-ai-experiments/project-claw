#!/usr/bin/env python3
"""Install the two projectclaw cron jobs (daily standup + weekly summary).

Idempotent: re-running won't create duplicates — jobs are matched by name.

Usage:
    projectclaw onboard          # one-time, also sets up workspace
    python slack-app/install_cron.py [--workspace PATH]

If --workspace is omitted, the script reads the current workspace from
the active nanobot config (~/.projectclaw/config.json by default).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make the repo importable when running this file directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nanobot.cron.service import CronService  # noqa: E402
from nanobot.cron.types import CronSchedule  # noqa: E402

# --- The two jobs ---------------------------------------------------------

DAILY_STANDUP_NAME = "projectclaw-daily-standup"
DAILY_STANDUP_SCHEDULE = "0 9 * * 2-5"  # Tue-Fri 9am
DAILY_STANDUP_MESSAGE = (
    "Post the daily project standup for projectclaw. Include: "
    "(1) open PRs awaiting review, (2) PRs merged in the last 24 hours. "
    "Be concise; cite each PR with its number and URL. Skip empty sections."
)

WEEKLY_SUMMARY_NAME = "projectclaw-weekly-summary"
WEEKLY_SUMMARY_SCHEDULE = "0 9 * * 1"  # Mon 9am
WEEKLY_SUMMARY_MESSAGE = (
    "Post the weekly project summary for projectclaw. Include: "
    "(1) all PRs merged in the last 7 days, "
    "(2) open PRs aging — oldest first, only those open more than 3 days, "
    "(3) issues opened or closed in the last 7 days. "
    "Cite every item with its number and URL. Be punchy."
)

# Target Slack channel: #gies-disruption-lab (id is stable, name can be renamed)
TARGET_CHANNEL = "slack"
TARGET_CHAT_ID = "C0B6FAWLRA7"


def _workspace_from_config() -> Path:
    """Resolve the workspace path from the active config."""
    from nanobot.config.loader import load_config

    cfg = load_config()
    workspace = cfg.agents.defaults.workspace
    if not workspace:
        raise SystemExit(
            "No workspace configured. Run `projectclaw onboard` first, or pass --workspace."
        )
    return Path(workspace).expanduser()


def install(workspace: Path, *, verbose: bool = True) -> dict[str, str]:
    """Add the two jobs to the workspace's cron store if not already present.

    Returns a dict mapping job-name -> outcome ('added' or 'already exists').
    """
    store_path = workspace / "cron" / "jobs.json"
    store_path.parent.mkdir(parents=True, exist_ok=True)
    service = CronService(store_path)

    existing = {j.name for j in service.list_jobs(include_disabled=True)}

    outcomes: dict[str, str] = {}

    for name, expr, message in (
        (DAILY_STANDUP_NAME, DAILY_STANDUP_SCHEDULE, DAILY_STANDUP_MESSAGE),
        (WEEKLY_SUMMARY_NAME, WEEKLY_SUMMARY_SCHEDULE, WEEKLY_SUMMARY_MESSAGE),
    ):
        if name in existing:
            outcomes[name] = "already exists"
            if verbose:
                print(f"  {name}: already exists, skipped")
            continue
        service.add_job(
            name=name,
            schedule=CronSchedule(kind="cron", expr=expr),
            message=message,
            deliver=True,
            channel=TARGET_CHANNEL,
            to=TARGET_CHAT_ID,
        )
        outcomes[name] = "added"
        if verbose:
            print(f"  {name}: added ({expr})")

    return outcomes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="Workspace path (defaults to the active config's workspace).",
    )
    args = parser.parse_args()

    workspace = args.workspace.expanduser() if args.workspace else _workspace_from_config()
    print(f"Installing projectclaw cron jobs into {workspace}/cron/jobs.json")
    install(workspace)
    print("Done. Restart the gateway for changes to take effect.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
