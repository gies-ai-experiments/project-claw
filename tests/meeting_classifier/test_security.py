from nanobot.integrations import safe_external_error
from nanobot.meeting_classifier.repository import _sanitize_error


def test_external_error_format_never_reflects_secret_inputs() -> None:
    token = "tok_1234567890abcdefghijklmnopqrstuvwxyz"
    dsn = "postgresql://user:super-secret@example.invalid/projectclaw"
    message = safe_external_error("Asana", "create task", 401)
    stored = _sanitize_error(f"Authorization: Bearer {token}\nurl={dsn}")

    assert message == "Asana create task failed (HTTP 401)."
    assert token not in stored
    assert "super-secret" not in stored
