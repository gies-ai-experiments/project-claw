from types import SimpleNamespace

import pytest

from nanobot.channels.slack import SlackConfig
from nanobot.config.schema import Config, Project
from nanobot.store import database


@pytest.mark.asyncio
async def test_database_only_bootstrap_migrates_seeds_and_hydrates(monkeypatch) -> None:
    events: list[str] = []
    pool = SimpleNamespace(acquire=lambda: None)

    class Acquire:
        async def __aenter__(self):
            return "conn"

        async def __aexit__(self, *_args):
            return None

    pool.acquire = Acquire
    monkeypatch.setattr(database, "init_pool", lambda _dsn: _async_value(pool))
    monkeypatch.setattr(database, "apply_migrations", lambda _conn: _async_event(events, "migrate"))

    class Registry:
        def __init__(self, _conn):
            pass

        async def seed_static(self, _cfg):
            events.append("seed")

        async def load_dynamic(self):
            return [Project(name="new-lab", asana={"projectGid": "A1"}, channel="CNEW")]

    monkeypatch.setattr(database, "RuntimeProjectRegistry", Registry)
    config = Config.model_validate({"database": {"dsn": "postgresql://db/projectclaw"}})
    slack = SlackConfig()
    assert await database.setup_database(config, slack) is pool
    assert events == ["migrate", "seed"]
    assert slack.projects["new-lab"].asana.project_gid == "A1"
    assert slack.project_channels["CNEW"].default_project == "new-lab"


@pytest.mark.asyncio
async def test_database_bootstrap_returns_none_without_any_dsn() -> None:
    assert await database.setup_database(Config(), SlackConfig()) is None


async def _async_value(value):
    return value


async def _async_event(events, value):
    events.append(value)
