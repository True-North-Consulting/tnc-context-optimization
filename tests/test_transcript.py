from __future__ import annotations

import pytest
from conftest import BOUNDARY, assistant, user, write_transcript

from tnc_context import HookError
from tnc_context.transcript import (TAIL_START, activity, agent_transcript, boundaries_since, grep,
                                    latest_call)


def test_the_latest_main_thread_call_with_its_model(isolated):
    transcript = write_transcript(isolated["tmp"] / "t.jsonl", [
        assistant(120000), user("next"), assistant(150000, model="claude-opus-5-5"),
        assistant(9000, sidechain=True),
    ])
    assert latest_call(transcript) == (150000, "claude-opus-5-5")


def test_after_a_compaction_the_context_is_unknown_until_the_next_call(isolated):
    path = isolated["tmp"] / "t.jsonl"
    write_transcript(path, [assistant(290000), BOUNDARY, user("go on")])
    assert latest_call(path) is None
    write_transcript(path, [assistant(290000), BOUNDARY, user("go on"), assistant(61000)])
    assert latest_call(path).tokens == 61000


def test_found_behind_tool_results_larger_than_the_first_tail(isolated):
    huge = {"type": "user", "message": {"content": [
        {"type": "tool_result", "content": "x" * (TAIL_START * 2)}]}}
    transcript = write_transcript(isolated["tmp"] / "t.jsonl", [assistant(180000), huge])
    assert latest_call(transcript).tokens == 180000


def test_a_failed_calls_stand_in_record_is_skipped(isolated):
    error = {"type": "assistant", "isApiErrorMessage": True,
             "message": {"content": [], "usage": {"input_tokens": 0, "output_tokens": 0}}}
    transcript = write_transcript(isolated["tmp"] / "t.jsonl", [assistant(260000), user("go"), error])
    assert latest_call(transcript).tokens == 260000


def test_a_transcript_without_calls(isolated):
    assert latest_call(write_transcript(isolated["tmp"] / "t.jsonl", [user("hello")])) is None


def test_activity_keeps_real_prompts_edits_and_commands(isolated):
    transcript = write_transcript(isolated["tmp"] / "t.jsonl", [
        user("Bitte den Export reparieren"),
        user("<command-name>/clear</command-name>"),
        user("[Request interrupted by user]"),
        user("meta note", isMeta=True),
        user("summary of before", isCompactSummary=True),
        user([{"type": "tool_result", "content": "output"}]),
        user([{"type": "text", "text": "und die Tests"}]),
        assistant(1000, tools=(("Edit", {"file_path": "/p/a.py"}), ("Bash", {"command": "pytest -q"}))),
        assistant(2000, tools=(("Write", {"file_path": "/p/b.py"}), ("Edit", {"file_path": "/p/a.py"}))),
        assistant(3000, sidechain=True, tools=(("Edit", {"file_path": "/p/sub.py"}),)),
    ])
    done = activity(transcript)
    assert done.prompts == ["Bitte den Export reparieren", "und die Tests"]
    assert done.edited == ["/p/b.py", "/p/a.py"]
    assert done.commands == ["pytest -q"]


def test_grep_finds_details_on_both_sides_of_a_compaction(isolated):
    tool_use = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": "gh pr view 4711"}}]}}
    result = {"type": "user", "message": {"content": [
        {"type": "tool_result", "content": [{"type": "text", "text": "Error: quota exceeded for PR 4711"}]}]}}
    transcript = write_transcript(isolated["tmp"] / "t.jsonl", [
        user("Bitte PR 4711 mergen"), tool_use, result, BOUNDARY, assistant(1000, text="4711 is merged"),
    ])
    lines = grep(transcript, "4711", limit=20, width=40).splitlines()
    assert len(lines) == 4
    assert lines[0].startswith("[compaction 0 · line 1] user:")
    assert "tool Bash" in lines[1] and "result" in lines[2]
    assert lines[3].startswith("[compaction 1 · line 5] claude:")


def test_grep_caps_its_output(isolated):
    transcript = write_transcript(isolated["tmp"] / "t.jsonl", [user(f"hit {i}") for i in range(30)])
    out = grep(transcript, "hit", limit=5, width=10).splitlines()
    assert len(out) == 6
    assert out[-1] == "(25 more matches — narrow the pattern)"


def test_grep_without_matches(isolated):
    transcript = write_transcript(isolated["tmp"] / "t.jsonl", [user("nothing")])
    assert grep(transcript, "absent", limit=5, width=10) == "no matches"


def test_a_subagents_transcript_is_read_as_its_own_conversation(isolated):
    transcript = write_transcript(isolated["tmp"] / "agent-a1.jsonl", [
        user("Implement package B2", isSidechain=True),
        assistant(40000, sidechain=True, tools=(("Edit", {"file_path": "/p/b2.py"}),)),
        user("<system-reminder>harness notice</system-reminder>", isSidechain=True, isMeta=True),
        user("The coordinator sent a message while you were working:\nRebase onto main",
             isSidechain=True, isMeta=True),
        assistant(45000, sidechain=True, model="claude-haiku-5-5"),
    ])
    assert latest_call(transcript) is None
    assert latest_call(transcript, sidechain=True) == (45000, "claude-haiku-5-5")
    done = activity(transcript, sidechain=True)
    assert done.prompts == ["Implement package B2", "Rebase onto main"]
    assert done.edited == ["/p/b2.py"]
    assert activity(transcript).prompts == []


def test_a_relayed_message_is_no_prompt_in_the_main_session(isolated):
    transcript = write_transcript(isolated["tmp"] / "t.jsonl", [
        user("The coordinator sent a message while you were working:\nhi", isMeta=True)])
    assert activity(transcript).prompts == []


def test_the_subagents_transcript_sits_beside_the_parents(isolated):
    parent = isolated["tmp"] / "s1.jsonl"
    own = isolated["tmp"] / "s1" / "subagents" / "agent-a1.jsonl"
    event = {"transcript_path": str(parent), "agent_id": "a1"}
    with pytest.raises(HookError, match="no transcript for subagent a1"):
        agent_transcript(event)
    own.parent.mkdir(parents=True)
    write_transcript(own, [user("go", isSidechain=True)])
    assert agent_transcript(event) == own
    given = write_transcript(isolated["tmp"] / "elsewhere.jsonl", [])
    assert agent_transcript(dict(event, agent_transcript_path=str(given))) == given


def test_boundaries_since_reads_only_complete_lines(isolated):
    import json
    path = write_transcript(isolated["tmp"] / "t.jsonl", [assistant(1000)])
    found, offset = boundaries_since(path, 0)
    assert found == 0 and offset == path.stat().st_size
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(BOUNDARY)[:15])
    assert boundaries_since(path, offset) == (0, offset)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(BOUNDARY)[15:] + "\n")
    assert boundaries_since(path, offset) == (1, path.stat().st_size)


def test_boundaries_since_skips_a_cut_line_and_a_rewritten_file(isolated):
    import json
    path = isolated["tmp"] / "t.jsonl"
    path.write_text('{"type": "system", "subtype": "compact_boundary", "cut\n', encoding="utf-8")
    assert boundaries_since(path, 0)[0] == 0
    write_transcript(path, [BOUNDARY])
    assert boundaries_since(path, 10_000) == (0, path.stat().st_size)  # rewritten: from here


def test_boundaries_since_counts_only_this_conversations_boundaries(isolated):
    path = write_transcript(isolated["tmp"] / "t.jsonl", [dict(BOUNDARY, isSidechain=True)])
    assert boundaries_since(path, 0)[0] == 0  # a subagent's, written into the session's file
    assert boundaries_since(path, 0, sidechain=True)[0] == 1
    plain = write_transcript(isolated["tmp"] / "agent-a1.jsonl", [BOUNDARY])
    assert boundaries_since(plain, 0, sidechain=True)[0] == 1  # its own file


def test_a_subagents_boundary_in_the_sessions_file_does_not_hide_its_context(isolated):
    path = write_transcript(isolated["tmp"] / "t.jsonl", [
        assistant(120000), dict(BOUNDARY, isSidechain=True)])
    assert latest_call(path).tokens == 120000
