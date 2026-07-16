from nanobot.config.schema import (
    DailyDigestConfig,
    DailyDigestProjectConfig,
    GatewayConfig,
    Project,
)


def test_project_accepts_daily_digest_camelcase():
    p = Project.model_validate({
        "name": "claw",
        "github": {"repos": ["o/r"]},
        "dailyDigest": {"enabled": True, "digestChannel": "C123"},
    })
    assert p.daily_digest.enabled is True
    assert p.daily_digest.digest_channel == "C123"


def test_daily_digest_defaults_off():
    assert DailyDigestProjectConfig().enabled is False
    assert DailyDigestProjectConfig().digest_channel == ""


def test_gateway_daily_digest_defaults():
    g = GatewayConfig()
    assert g.daily_digest.enabled is False
    assert g.daily_digest.cron == "0 9 * * *"
