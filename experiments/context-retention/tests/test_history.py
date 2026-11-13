"""Chat Completions message validity, grouping and compaction planning."""

import pytest

from ctxlab import prompts
from ctxlab.history import ConversationState, InvalidHistory, groups, plan_compaction, render_transcript, validate

SYS = {"role": "system", "content": "s"}
TASK = {"role": "user", "content": "t"}


def call(i, name="read_artifact", args='{"id":"ticket"}'):
    return {"id": f"c{i}", "type": "function", "function": {"name": name, "arguments": args}}


def asst(*ids, content=None):
    return {"role": "assistant", "content": content, "tool_calls": [call(i) for i in ids]}


def tool(i, content="x"):
    return {"role": "tool", "tool_call_id": f"c{i}", "content": content}


def test_valid_history_passes():
    validate([SYS, TASK, asst(1, 2), tool(1), tool(2), asst(3), tool(3), {"role": "assistant", "content": "done"}])


@pytest.mark.parametrize("messages", [
    [TASK],  # no system first
    [SYS, TASK, asst(1)],  # unanswered at end
    [SYS, TASK, asst(1), TASK],  # unanswered before next message
    [SYS, TASK, tool(1)],  # orphan result
    [SYS, TASK, asst(1), tool(1), asst(1), tool(1)],  # duplicate id
    [SYS, TASK, asst(1), tool(2)],  # wrong id
    [SYS, TASK, {"role": "assistant", "content": None}],  # empty assistant
    [SYS, TASK, asst(1), {"role": "tool", "tool_call_id": "c1", "content": None}],  # non-string result
])
def test_invalid_histories_are_rejected(messages):
    with pytest.raises(InvalidHistory):
        validate(messages)


def test_groups_require_complete_tool_answers():
    assert len(groups([asst(1, 2), tool(1), tool(2), {"role": "assistant", "content": "a"}])) == 2
    with pytest.raises(InvalidHistory):
        groups([asst(1, 2), tool(1)])


def test_plan_keeps_recent_groups_within_budget_and_summarizes_the_rest():
    big = "y" * 3_000  # ~1000 tokens at 3 chars/token
    state = ConversationState([SYS, TASK], body=[asst(1), tool(1, big), asst(2), tool(2, big), asst(3), tool(3, big)])
    plan = plan_compaction(state, keep_recent_groups=2, retain_budget_tokens=2_500, chars_per_token=3.0)
    assert plan.retained_groups == 2 and plan.summarized_groups == 1
    assert [m.get("tool_call_id") for m in plan.retained if m["role"] == "tool"] == ["c2", "c3"]
    tight = plan_compaction(state, keep_recent_groups=2, retain_budget_tokens=1_100, chars_per_token=3.0)
    assert tight.retained_groups == 1 and tight.summarized_groups == 2


def test_plan_keeps_the_latest_group_even_over_budget_and_returns_none_without_older_groups():
    state = ConversationState([SYS, TASK], body=[asst(1), tool(1, "z" * 90_000)])
    assert plan_compaction(state, 2, 10, 3.0) is None
    state.body = [asst(0), tool(0), *state.body]
    plan = plan_compaction(state, 2, 10, 3.0)
    assert plan.retained_groups == 1 and plan.summarized_groups == 1


def test_state_roundtrip_with_summary():
    msgs = [SYS, TASK, {"role": "user", "content": prompts.summary_message(1, 4, "SUMMARY BODY")}, asst(5), tool(5)]
    st = ConversationState.from_messages(msgs)
    assert st.summary == "SUMMARY BODY" and len(st.body) == 2 and st.messages() == msgs


def test_transcript_rendering_keeps_full_tool_results():
    text = render_transcript([asst(1, content="thinking"), tool(1, "R" * 10_000)])
    assert "TOOL CALL c1: read_artifact" in text and "R" * 10_000 in text and "ASSISTANT: thinking" in text
