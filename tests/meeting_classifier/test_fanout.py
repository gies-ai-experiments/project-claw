from nanobot.channels.slack import SlackChannel
from nanobot.meeting_classifier.fanout import (
    build_approval,
    button_value,
    format_post,
    parse_action,
    parse_classification,
    parse_structured_classification,
)

KNOWN = {"atlas", "glp-v2"}


def test_parse_classification_filters_unknown_projects():
    content = '[{"project":"atlas","summary":"s","actions":["a"]},' \
              '{"project":"ghost","summary":"x"}]'
    out = parse_classification(content, KNOWN)
    assert len(out) == 1
    assert out[0]["project"] == "atlas" and out[0]["actions"] == ["a"]


def test_parse_classification_tolerates_fence_and_bad_json():
    assert parse_classification("```json\n[{\"project\":\"atlas\"}]\n```", KNOWN)[0]["project"] == "atlas"
    assert parse_classification("not json", KNOWN) == []
    assert parse_classification("[]", KNOWN) == []


def test_parse_action_roundtrip():
    v = button_value("approve", "not_1", "glp-v2")
    assert parse_action(v) == ("approve", "not_1", "glp-v2")
    assert parse_action(button_value("skip", "not_9", "atlas")) == ("skip", "not_9", "atlas")


def test_parse_action_ignores_foreign_values():
    assert parse_action("Approve") is None
    assert parse_action("mtg-approve:") is None
    assert parse_action("") is None


def test_build_approval_empty_when_no_drafts():
    assert build_approval("t", "n1", []) == ("", [])


def test_build_approval_makes_per_project_buttons():
    drafts = [{"project": "atlas", "summary": "did x", "actions": ["a1"]}]
    text, buttons = build_approval("Standup", "not_1", drafts)
    assert "atlas" in text and "did x" in text
    # one row with Approve + Skip carrying the encoded values
    assert buttons[0][0][1] == "mtg-approve:not_1:atlas"
    assert buttons[0][1][1] == "mtg-skip:not_1:atlas"


def test_button_blocks_support_label_value_pairs_with_unique_action_ids():
    _, buttons = build_approval("Standup", "not_1", [
        {"project": "atlas", "summary": "s"},
        {"project": "glp-v2", "summary": "s"},
    ])
    blocks = SlackChannel._build_button_blocks("hi", buttons)
    elements = blocks[1]["elements"]
    # labels are friendly, values carry the encoded action
    assert elements[0]["text"]["text"].startswith("✓ Approve")
    assert elements[0]["value"] == "mtg-approve:not_1:atlas"
    action_ids = [e["action_id"] for e in elements]
    assert len(action_ids) == len(set(action_ids))  # all unique


def test_build_button_blocks_legacy_string_still_works():
    blocks = SlackChannel._build_button_blocks("hi", [["Approve", "Reject"]])
    els = blocks[1]["elements"]
    assert els[0]["value"] == "Approve" and els[1]["value"] == "Reject"


def test_format_post_includes_summary_and_actions():
    out = format_post("atlas", "Standup", {"summary": "did x", "actions": ["a1", "a2"]})
    assert "atlas" in out and "did x" in out and "a1" in out and "a2" in out


def test_parser_accepts_existing_and_new_project_drafts():
    raw = """[
      {"project":"atlas","isNewProject":false,"summary":"Existing","tasks":[]},
      {"project":"new-lab","isNewProject":true,"displayName":"New Lab",
       "description":"Research project","channelSlug":"new-lab",
       "lead":{"name":"Lead","email":"lead@example.edu"},
       "summary":"Initial meeting","tasks":[
         {"id":"t1","title":"Draft charter","owner":null,
          "collaborators":[],"dueOn":null,"dueOnSource":null}
       ]}
    ]"""

    drafts = parse_structured_classification(raw, {"atlas"})

    assert [draft.project for draft in drafts] == ["atlas", "new-lab"]
    assert drafts[1].is_new_project is True


def test_structured_parser_strips_one_optional_fence():
    raw = '```json\n[{"project":"atlas","tasks":[]}]\n```'
    assert parse_structured_classification(raw, KNOWN)[0].project == "atlas"


def test_structured_parser_drops_invalid_and_unknown_entries():
    raw = """[
      {"project":"atlas","tasks":[]},
      {"project":"ghost","tasks":[]},
      {"project":"new-lab","isNewProject":true,"tasks":[]},
      {"project":"atlas","tasks":[{"id":"t1","title":" "}]},
      "not-an-object"
    ]"""
    assert [draft.project for draft in parse_structured_classification(raw, KNOWN)] == ["atlas"]


def test_structured_parser_rejects_malformed_top_level_json():
    assert parse_structured_classification("not json", KNOWN) == []
    assert parse_structured_classification('{"project":"atlas"}', KNOWN) == []
