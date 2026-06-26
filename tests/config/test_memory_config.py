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


def test_distiller_defaults():
    cfg = MemoryConfig(dsn="postgresql://x")
    assert cfg.distiller_enabled is True
    assert cfg.distiller_model == "openai/gpt-5.5"
    assert cfg.distiller_cron == "0 3 * * *"
    assert cfg.distiller_batch_messages == 50


def test_distiller_active_requires_memory_active():
    assert MemoryConfig().distiller_active is False  # no dsn
    assert MemoryConfig(dsn="postgresql://x").distiller_active is True
    assert MemoryConfig(dsn="postgresql://x", distiller_enabled=False).distiller_active is False


def test_distiller_schedule_builds_cron_schedule():
    from nanobot.cron.types import CronSchedule

    sched = MemoryConfig(dsn="postgresql://x").distiller_schedule("America/Vancouver")
    assert isinstance(sched, CronSchedule)
    assert sched.kind == "cron"
    assert sched.expr == "0 3 * * *"
    assert sched.tz == "America/Vancouver"


def test_distiller_config_accepts_camel_and_snake():
    cfg = MemoryConfig.model_validate(
        {"dsn": "postgresql://x", "distillerModel": "openai/gpt-5.2", "distillerCron": "0 4 * * *",
         "distillerBatchMessages": 10}
    )
    assert cfg.distiller_model == "openai/gpt-5.2"
    assert cfg.distiller_cron == "0 4 * * *"
    assert cfg.distiller_batch_messages == 10
