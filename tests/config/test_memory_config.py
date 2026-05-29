"""Tests for the memory feature-flag config."""
from __future__ import annotations

from nanobot.config.schema import Config, MemoryConfig


def test_memory_config_defaults_enabled_no_dsn():
    cfg = Config.model_validate({})
    assert cfg.memory.enabled is True
    assert cfg.memory.dsn is None
    assert cfg.memory.inject_limit == 20


def test_memory_config_accepts_camel_and_snake():
    cfg = Config.model_validate(
        {"memory": {"enabled": False, "dsn": "postgresql://x", "injectLimit": 5}}
    )
    assert cfg.memory.enabled is False
    assert cfg.memory.dsn == "postgresql://x"
    assert cfg.memory.inject_limit == 5


def test_memory_config_active_requires_dsn():
    assert MemoryConfig().active is False  # enabled but no dsn -> inert
    assert MemoryConfig(enabled=True, dsn="postgresql://x").active is True
    assert MemoryConfig(enabled=False, dsn="postgresql://x").active is False
