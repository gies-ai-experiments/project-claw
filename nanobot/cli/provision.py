"""Dev-side one-time Slack channel provisioning, one per project.

Runtime cannot persist created channel IDs (prod config is image-baked), so this
prints a mapping you paste into config.json before building the image.
"""
from __future__ import annotations

import re
from typing import Any

from nanobot.config.schema import Project


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", name.strip().lower()).strip("-")


async def provision_channels(
    web: Any, projects: list[Project], existing: dict[str, str], dry_run: bool
) -> list[dict]:
    out: list[dict] = []
    for p in projects:
        if p.name in existing:
            out.append({"project": p.name, "channel_id": existing[p.name], "created": False})
            continue
        if dry_run:
            out.append({"project": p.name, "channel_id": "", "created": False})
            continue
        resp = await web.conversations_create(name=slugify(p.name))
        cid = resp["channel"]["id"]
        for person in p.people:
            if person.slack_id:
                await web.conversations_invite(channel=cid, users=person.slack_id)
        out.append({"project": p.name, "channel_id": cid, "created": True})
    return out
