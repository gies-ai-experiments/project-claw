import pytest
from pydantic import ValidationError

from nanobot.config.schema import Config, Project


def test_asana_config_accepts_camel_case_and_asana_only_project():
    cfg = Config.model_validate({
        "database": {"dsn": "postgresql://db/projectclaw"},
        "integrations": {"asana": {
            "enabled": True,
            "accessToken": "secret",
            "workspaceGid": "w1",
            "teamGid": "t1",
        }},
    })
    project = Project.model_validate({
        "name": "new-project",
        "asana": {"projectGid": "p1"},
        "leadEmail": "lead@example.edu",
        "people": [{"email": "lead@example.edu", "asanaUserGid": "u1"}],
    })
    assert cfg.integrations.asana.active is True
    assert project.asana.project_gid == "p1"
    assert project.people[0].asana_user_gid == "u1"


def test_asana_requires_general_database_and_credentials():
    with pytest.raises(ValidationError, match="database.dsn"):
        Config.model_validate({"integrations": {"asana": {"enabled": True}}})


def test_database_dsn_does_not_enable_memory():
    cfg = Config.model_validate({
        "database": {"dsn": "postgresql://db/projectclaw"},
        "memory": {"enabled": True},
    })
    assert cfg.memory.active is False


def test_two_different_dsns_are_rejected():
    with pytest.raises(ValidationError, match="same Postgres DSN"):
        Config.model_validate({
            "database": {"dsn": "postgresql://db/one"},
            "memory": {"dsn": "postgresql://db/two"},
        })
