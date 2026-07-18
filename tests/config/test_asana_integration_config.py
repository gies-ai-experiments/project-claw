import json

import pytest
from pydantic import ValidationError

from nanobot.config.loader import save_config
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


def test_database_and_asana_secrets_are_not_serialized(tmp_path):
    config_path = tmp_path / "config.json"
    cfg = Config.model_validate({
        "database": {"dsn": "postgresql://sentinel-dsn"},
        "integrations": {"asana": {
            "enabled": True,
            "accessToken": "sentinel-access-token",
            "workspaceGid": "w1",
            "teamGid": "t1",
        }},
    })

    dumped = cfg.model_dump(mode="json", by_alias=True)
    assert "dsn" not in dumped["database"]
    assert "accessToken" not in dumped["integrations"]["asana"]

    save_config(cfg, config_path)
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert "dsn" not in saved["database"]
    assert "accessToken" not in saved["integrations"]["asana"]


def test_database_and_asana_secrets_are_not_exposed_by_repr_or_validation_errors():
    dsn = "postgresql://sentinel-dsn"
    access_token = "sentinel-access-token"
    cfg = Config.model_validate({
        "database": {"dsn": dsn},
        "integrations": {"asana": {
            "enabled": True,
            "accessToken": access_token,
            "workspaceGid": "w1",
            "teamGid": "t1",
        }},
    })
    assert dsn not in repr(cfg)
    assert access_token not in repr(cfg)

    with pytest.raises(ValidationError) as exc_info:
        Config.model_validate({
            "database": {"dsn": dsn},
            "integrations": {"asana": {
                "enabled": True,
                "accessToken": access_token,
                "workspaceGid": "w1",
                "teamGid": " ",
            }},
        })
    error_text = str(exc_info.value)
    assert dsn not in error_text
    assert access_token not in error_text
    assert "input_value=" not in error_text
    structured_errors = repr(exc_info.value.errors())
    assert dsn not in structured_errors
    assert access_token not in structured_errors
    assert exc_info.value.errors()[0]["loc"] == ()
    assert "integrations.asana.team_gid" in exc_info.value.errors()[0]["msg"]


def test_direct_config_construction_redacts_environment_secrets_from_errors(monkeypatch):
    dsn = "postgresql://environment-sentinel-dsn"
    access_token = "environment-sentinel-access-token"
    monkeypatch.setenv("NANOBOT_DATABASE__DSN", dsn)
    monkeypatch.setenv("NANOBOT_INTEGRATIONS__ASANA__ENABLED", "true")
    monkeypatch.setenv("NANOBOT_INTEGRATIONS__ASANA__ACCESS_TOKEN", access_token)
    monkeypatch.setenv("NANOBOT_INTEGRATIONS__ASANA__WORKSPACE_GID", " ")
    monkeypatch.setenv("NANOBOT_INTEGRATIONS__ASANA__TEAM_GID", "t1")

    with pytest.raises(ValidationError) as exc_info:
        Config()

    structured_errors = repr(exc_info.value.errors())
    assert dsn not in structured_errors
    assert access_token not in structured_errors
    assert exc_info.value.errors()[0]["loc"] == ()
    assert "integrations.asana.workspace_gid" in exc_info.value.errors()[0]["msg"]


@pytest.mark.parametrize(
    ("field", "diagnostic"),
    [
        ("database.dsn", "database.dsn"),
        ("accessToken", "integrations.asana.access_token"),
        ("workspaceGid", "integrations.asana.workspace_gid"),
        ("teamGid", "integrations.asana.team_gid"),
    ],
)
def test_asana_rejects_whitespace_only_required_fields(field, diagnostic):
    data = {
        "database": {"dsn": "postgresql://db/projectclaw"},
        "integrations": {"asana": {
            "enabled": True,
            "accessToken": "secret",
            "workspaceGid": "w1",
            "teamGid": "t1",
        }},
    }
    if field == "database.dsn":
        data["database"]["dsn"] = " \t "
    else:
        data["integrations"]["asana"][field] = " \t "

    with pytest.raises(ValidationError, match=diagnostic):
        Config.model_validate(data)
