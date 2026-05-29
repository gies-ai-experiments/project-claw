"""build_messages should inject the L1 conversation-memory block as a system msg."""
from __future__ import annotations

from nanobot.agent.context import ContextBuilder


def test_conversation_memory_injected_after_system_before_user(tmp_path):
    cb = ContextBuilder(tmp_path)
    block = "[Conversation Memory]\n[user] earlier q\n[assistant] earlier a"
    messages = cb.build_messages(
        history=[],
        current_message="new question",
        channel="slack",
        chat_id="C1",
        conversation_memory=block,
    )
    # first message is the main system prompt, second is the memory block
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "system"
    assert messages[1]["content"] == block
    # the live user turn comes after the memory block
    assert messages[-1]["role"] == "user"
    assert "new question" in (
        messages[-1]["content"]
        if isinstance(messages[-1]["content"], str)
        else str(messages[-1]["content"])
    )


def test_no_conversation_memory_means_no_extra_system_message(tmp_path):
    cb = ContextBuilder(tmp_path)
    messages = cb.build_messages(
        history=[], current_message="hi", channel="slack", chat_id="C1"
    )
    system_msgs = [m for m in messages if m["role"] == "system"]
    assert len(system_msgs) == 1
