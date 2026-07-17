from nanobot.config.schema import (
    GatewayConfig,
    GitHubProjectConfig,
    MeetingClassifierConfig,
    Project,
)


def test_project_accepts_channel_and_description_camelcase():
    p = Project.model_validate({
        "name": "atlas",
        "github": {"repos": ["gies-ai-hub/Atlas"]},
        "channel": "C0BJ59YLQ58",
        "description": "portfolio visualization",
    })
    assert p.channel == "C0BJ59YLQ58"
    assert p.description == "portfolio visualization"


def test_project_channel_description_default_empty():
    p = Project(name="x", github=GitHubProjectConfig(repos=["o/r"]))
    assert p.channel == "" and p.description == ""


def test_meeting_classifier_defaults():
    c = MeetingClassifierConfig()
    assert c.enabled is False
    assert c.folder_id == ""
    assert c.admin_slack_id == ""
    assert c.interval_s == 900


def test_gateway_has_meeting_classifier_camelcase():
    g = GatewayConfig.model_validate({
        "meetingClassifier": {"enabled": True, "folderId": "fol_1", "adminSlackId": "U1"}
    })
    assert g.meeting_classifier.enabled is True
    assert g.meeting_classifier.folder_id == "fol_1"
    assert g.meeting_classifier.admin_slack_id == "U1"
