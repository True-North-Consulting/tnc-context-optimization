from __future__ import annotations

import json
import os

import pytest
from conftest import BOUNDARY, SESSION, assistant, user, write_transcript

from tnc_context import HookError
from tnc_context.ledger import agent_start, agent_stop, ledger_path, session_start, state_path
from tnc_context.watch import watch


SIDE_BOUNDARY = dict(BOUNDARY, isSidechain=True)


def event(transcript, **extra) -> dict:
    return {"session_id": SESSION, "transcript_path": str(transcript),
            "hook_event_name": "PostToolBatch", **extra}


def at(isolated, *tokens: int):
    """Writes a transcript whose latest call has `tokens[-1]` of context."""
    return write_transcript(isolated["tmp"] / "t.jsonl", [assistant(t) for t in tokens])


def compacted(path, records: list) -> None:
    """The transcript after a compaction, and the SessionStart that follows it."""
    write_transcript(path, records)
    session_start({"session_id": SESSION, "source": "compact", "transcript_path": str(path)})


def write_ledger(when: float) -> None:
    ledger = ledger_path(SESSION)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("## Goal", encoding="utf-8")
    os.utime(ledger, (when, when))


def test_quiet_below_the_warning(isolated, window):
    assert watch(event(at(isolated, 220000))) is None


def test_warns_before_the_compaction_point_not_at_the_window(isolated, window):
    note = watch(event(at(isolated, 228000)))
    assert note.startswith("Context is at 228k; auto-compaction comes at about 267k.")
    assert str(ledger_path(SESSION)) in note


def test_an_ignored_warning_is_repeated_once_more_firmly(isolated, window):
    assert watch(event(at(isolated, 230000))) is not None
    assert watch(event(at(isolated, 232000))) is None
    assert watch(event(at(isolated, 234000))) is None
    again = watch(event(at(isolated, 236000)))
    assert "still not updated since the warning" in again and "31k away" in again
    assert watch(event(at(isolated, 238000))) is None
    assert watch(event(at(isolated, 240000))) is None


def test_a_ledger_written_after_the_warning_ends_it(isolated, window):
    write_ledger(1000)
    assert watch(event(at(isolated, 230000))) is not None
    write_ledger(2000)
    for tokens in (232000, 234000, 236000, 238000):
        assert watch(event(at(isolated, tokens))) is None


def test_the_warning_rearms_after_a_compaction(isolated, window):
    path = isolated["tmp"] / "t.jsonl"
    assert watch(event(at(isolated, 230000))) is not None
    compacted(path, [assistant(270000), BOUNDARY, user("go on")])
    assert watch(event(path)) is None  # no call since the compaction yet
    write_transcript(path, [assistant(270000), BOUNDARY, user("go on"), assistant(70000)])
    assert watch(event(path)) is None
    assert "Context is at 229k" in watch(event(at(isolated, 70000, 229000)))


def test_a_failed_call_does_not_rearm_the_warning(isolated, window):
    path = at(isolated, 230000)
    assert watch(event(path)) is not None
    error = {"type": "assistant", "isApiErrorMessage": True,
             "message": {"content": [], "usage": {"input_tokens": 0, "output_tokens": 0}}}
    write_transcript(path, [assistant(230000), error])
    write_ledger(5000)
    assert watch(event(path)) is None


def test_the_window_set_for_the_model_counts(isolated):
    (isolated["home"] / ".claude" / "settings.json").write_text(json.dumps(
        {"modelSettings": {"claude-test": {"autoCompactWindow": 200000}}}), encoding="utf-8")
    assert "auto-compaction comes at about 167k" in watch(event(at(isolated, 140000)))


def subagent(isolated, records: list, agent: str = "sub1") -> dict:
    """A subagent's PostToolBatch: the parent's transcript_path, its own file beside it."""
    parent = write_transcript(isolated["tmp"] / f"{SESSION}.jsonl", [assistant(50000)])
    folder = isolated["tmp"] / SESSION / "subagents"
    folder.mkdir(parents=True, exist_ok=True)
    write_transcript(folder / f"agent-{agent}.jsonl", records)
    return event(parent, agent_id=agent, agent_type="general-purpose")


def test_a_subagent_is_warned_with_its_own_ledger_and_state(isolated, window):
    note = watch(subagent(isolated, [assistant(230000, sidechain=True)]))
    assert note.startswith("Context is at 230k;")
    assert f"Bring your ledger ({ledger_path(SESSION, 'sub1')})" in note
    assert state_path(SESSION, "sub1").exists() and not state_path(SESSION).exists()


def test_a_subagent_without_its_transcript_fails_loudly(isolated, window):
    parent = write_transcript(isolated["tmp"] / f"{SESSION}.jsonl", [assistant(50000)])
    with pytest.raises(HookError, match="no transcript for subagent sub1"):
        watch(event(parent, agent_id="sub1"))


def test_the_subagents_ledger_comes_back_once_after_its_compaction(isolated, window):
    ledger = ledger_path(SESSION, "sub1")
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("## Goal\nPackage B2\n", encoding="utf-8")
    before = [user("Implement package B2", isSidechain=True), assistant(200000, sidechain=True)]
    assert watch(subagent(isolated, before)) is None
    after = before + [SIDE_BOUNDARY, user("summary", isSidechain=True, isCompactSummary=True),
                      assistant(30000, sidechain=True)]
    note = watch(subagent(isolated, after))
    assert note.startswith("Auto-compaction has just replaced the earlier conversation")
    assert "Package B2" in note and "“Implement package B2”" in note
    assert watch(subagent(isolated, after + [assistant(31000, sidechain=True)])) is None


def test_a_boundary_still_being_written_is_seen_next_time(isolated, window):
    ledger = ledger_path(SESSION, "sub1")
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("## Goal\nPackage B2\n", encoding="utf-8")
    hook = subagent(isolated, [assistant(200000, sidechain=True)])
    assert watch(hook) is None
    path = isolated["tmp"] / SESSION / "subagents" / "agent-sub1.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(assistant(200500, sidechain=True)) + "\n" + json.dumps(SIDE_BOUNDARY)[:20])
    assert watch(hook) is None
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(SIDE_BOUNDARY)[20:] + "\n" + json.dumps(assistant(30000, sidechain=True)) + "\n")
    assert "Package B2" in watch(hook)


def test_a_subagents_definition_can_lower_its_window(isolated, window):
    agents = isolated["project"] / ".claude" / "agents"
    agents.mkdir()
    (agents / "reader.md").write_text("---\nname: reader\nautoCompactWindow: 100000\n---\nRead.\n",
                                      encoding="utf-8")
    agent_start({"session_id": SESSION, "agent_id": "sub1", "agent_type": "reader"})
    assert "auto-compaction comes at about 67k" in watch(subagent(isolated, [assistant(55000, sidechain=True)]))


def test_without_a_window_no_warning(isolated):
    assert watch(event(at(isolated, 900000))) is None


def test_freshness_counts_from_the_last_ledger_write(isolated):
    write_ledger(1000)
    seen = [watch(event(at(isolated, tokens))) for tokens in (100000, 150000, 205000, 250000, 306000)]
    assert seen[0] is None and seen[1] is None and seen[3] is None
    assert "105k tokens of conversation back" in seen[2] and str(ledger_path(SESSION)) in seen[2]
    assert "206k tokens of conversation back" in seen[4]


def test_rewriting_the_ledger_restarts_the_count(isolated):
    write_ledger(1000)
    assert watch(event(at(isolated, 100000))) is None
    assert watch(event(at(isolated, 201000))) is not None
    write_ledger(2000)
    assert watch(event(at(isolated, 210000))) is None
    assert watch(event(at(isolated, 300000))) is None
    assert "101k tokens of conversation back" in watch(event(at(isolated, 311000)))


def test_a_session_without_a_ledger_is_asked_for_one(isolated):
    assert watch(event(at(isolated, 50000))) is None
    assert watch(event(at(isolated, 100000))) is None
    assert "grown by 101k without a session ledger" in watch(event(at(isolated, 151000)))


def test_the_warning_and_the_reminder_never_stack(isolated, window):
    assert watch(event(at(isolated, 120000))) is None
    note = watch(event(at(isolated, 230000)))
    assert note.startswith("Context is at 230k") and "grown by" not in note


def test_after_a_compaction_the_count_starts_from_the_smaller_context(isolated):
    path = isolated["tmp"] / "t.jsonl"
    assert watch(event(at(isolated, 200000))) is None
    compacted(path, [assistant(200000), BOUNDARY, user("go on"), assistant(60000)])
    assert watch(event(path)) is None
    assert watch(event(at(isolated, 60000, 155000))) is None
    assert "grown by 101k" in watch(event(at(isolated, 60000, 161000)))


def test_a_state_file_from_an_older_version_still_reads(isolated, window):
    path = state_path(SESSION)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"warned": False, "ledger_written": None, "ledger_tokens": 100000,
                                "nudged_tokens": 100000}), encoding="utf-8")
    assert watch(event(at(isolated, 230000))) is not None
    assert json.loads(path.read_text())["warned"] is True


def test_a_bad_session_id_writes_no_state(isolated, window):
    with pytest.raises(HookError):
        watch({"session_id": "../x", "transcript_path": str(at(isolated, 290000))})
    assert not (isolated["data"] / "state").exists()


def test_a_compaction_while_a_subagent_runs_comes_back_with_the_next_batch(isolated, window):
    """SessionStart cannot tell it from the subagent's, so it leaves it to watch."""
    ledger = ledger_path(SESSION)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("## Goal\nShip the export fix\n", encoding="utf-8")
    path = isolated["tmp"] / "t.jsonl"
    write_transcript(path, [user("go"), assistant(200000)])
    assert watch(event(path)) is None
    agent_start({"session_id": SESSION, "agent_id": "sub1"})
    records = [user("go"), assistant(200000), BOUNDARY, user("go on")]
    write_transcript(path, records)
    unsure = session_start({"session_id": SESSION, "source": "compact", "transcript_path": str(path)})
    assert "cannot tell whose compaction this was" in unsure and str(ledger) in unsure
    assert "Ship the export fix" not in unsure  # a subagent may be reading it
    note = watch(event(path, hook_event_name="UserPromptSubmit"))
    assert "Ship the export fix" in note and "“go”" in note
    assert watch(event(write_transcript(path, records + [assistant(30000)]))) is None


def test_with_no_subagent_running_the_session_start_brings_it_back_once(isolated, window):
    ledger = ledger_path(SESSION)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("## Goal\nShip the export fix\n", encoding="utf-8")
    path = isolated["tmp"] / "t.jsonl"
    write_transcript(path, [user("go"), assistant(200000)])
    assert watch(event(path)) is None
    agent_start({"session_id": SESSION, "agent_id": "sub1"})
    agent_stop({"session_id": SESSION, "agent_id": "sub1"})
    records = [user("go"), assistant(200000), BOUNDARY, user("go on")]
    write_transcript(path, records)
    assert "Ship the export fix" in session_start({"session_id": SESSION, "source": "compact",
                                                   "transcript_path": str(path)})
    assert watch(event(write_transcript(path, records + [assistant(30000)]))) is None


def test_a_new_sessions_first_prompt_comes_before_its_transcript(isolated, window):
    assert watch(event(isolated["tmp"] / "not-yet.jsonl", hook_event_name="UserPromptSubmit")) is None


def test_a_deferred_compaction_is_found_though_watch_never_ran(isolated, window):
    """SessionStart at startup sets where watch starts counting."""
    ledger = ledger_path(SESSION)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("## Goal\nShip the export fix\n", encoding="utf-8")
    path = write_transcript(isolated["tmp"] / "t.jsonl", [user("go"), assistant(200000)])
    session_start({"session_id": SESSION, "source": "startup", "transcript_path": str(path)})
    agent_start({"session_id": SESSION, "agent_id": "sub1"})
    records = [user("go"), assistant(200000), BOUNDARY, user("go on"), assistant(30000)]
    write_transcript(path, records)
    session_start({"session_id": SESSION, "source": "compact", "transcript_path": str(path)})
    assert "Ship the export fix" in watch(event(path))


def test_a_subagents_compaction_leaves_the_sessions_next_restore_alone(isolated, window):
    """Left pending by a subagent's compaction, the session restores only its own next one."""
    path = write_transcript(isolated["tmp"] / "t.jsonl", [user("go"), assistant(100000)])
    session_start({"session_id": SESSION, "source": "startup", "transcript_path": str(path)})
    agent_start({"session_id": SESSION, "agent_id": "sub1"})
    session_start({"session_id": SESSION, "source": "compact", "transcript_path": str(path)})
    assert watch(event(write_transcript(path, [user("go"), assistant(100000), assistant(110000)]))) is None
    agent_stop({"session_id": SESSION, "agent_id": "sub1"})
    records = [user("go"), assistant(100000), assistant(110000), BOUNDARY, user("go on")]
    write_transcript(path, records)
    assert "Recorded by the plugin" in session_start(
        {"session_id": SESSION, "source": "compact", "transcript_path": str(path)})
    assert watch(event(write_transcript(path, records + [assistant(30000)]))) is None


def test_a_resumed_session_forgets_subagents_of_its_previous_process(isolated, window):
    path = write_transcript(isolated["tmp"] / "t.jsonl", [user("go"), assistant(100000)])
    agent_start({"session_id": SESSION, "agent_id": "sub1"})
    session_start({"session_id": SESSION, "source": "resume", "transcript_path": str(path)})
    write_transcript(path, [user("go"), assistant(100000), BOUNDARY])
    assert "Recorded by the plugin" in session_start(
        {"session_id": SESSION, "source": "compact", "transcript_path": str(path)})


def test_a_warning_due_with_a_restore_comes_with_it(isolated, window):
    ledger = ledger_path(SESSION, "sub1")
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("## Goal\nPackage B2\n", encoding="utf-8")
    before = [user("Implement package B2", isSidechain=True), assistant(200000, sidechain=True)]
    assert watch(subagent(isolated, before)) is None
    note = watch(subagent(isolated, before + [SIDE_BOUNDARY, assistant(240000, sidechain=True)]))
    assert "Package B2" in note and "Context is at 240k" in note


def compact_start(path) -> str:
    return session_start({"session_id": SESSION, "source": "compact", "transcript_path": str(path)})


def test_a_restored_compaction_is_not_restored_again_after_a_subagents(isolated, window):
    path = write_transcript(isolated["tmp"] / "t.jsonl", [user("go"), assistant(100000)])
    session_start({"session_id": SESSION, "source": "startup", "transcript_path": str(path)})
    records = [user("go"), assistant(100000), BOUNDARY, user("go on")]
    write_transcript(path, records)
    assert "Recorded by the plugin" in compact_start(path)  # no subagent runs: at once
    agent_start({"session_id": SESSION, "agent_id": "sub1"})
    assert "cannot tell whose" in compact_start(path)  # the subagent's compaction
    assert watch(event(write_transcript(path, records + [assistant(30000)]))) is None


def test_a_compaction_left_before_a_resume_comes_back_after_it(isolated, window):
    ledger = ledger_path(SESSION)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("## Goal\nShip the export fix\n", encoding="utf-8")
    path = write_transcript(isolated["tmp"] / "t.jsonl", [user("go"), assistant(100000)])
    session_start({"session_id": SESSION, "source": "startup", "transcript_path": str(path)})
    agent_start({"session_id": SESSION, "agent_id": "sub1"})
    write_transcript(path, [user("go"), assistant(100000), BOUNDARY, user("go on")])
    assert "cannot tell whose" in compact_start(path)
    session_start({"session_id": SESSION, "source": "resume", "transcript_path": str(path)})
    assert "Ship the export fix" in watch(event(path, hook_event_name="UserPromptSubmit"))


def test_a_compaction_never_touches_the_state_watch_writes(isolated, window):
    """Two writers of one file lose updates; SessionStart keeps to its own."""
    path = write_transcript(isolated["tmp"] / "t.jsonl", [user("go"), assistant(100000)])
    session_start({"session_id": SESSION, "source": "startup", "transcript_path": str(path)})
    written = state_path(SESSION).read_text(encoding="utf-8")
    write_transcript(path, [user("go"), assistant(100000), BOUNDARY])
    compact_start(path)
    agent_start({"session_id": SESSION, "agent_id": "sub1"})
    compact_start(path)
    assert state_path(SESSION).read_text(encoding="utf-8") == written


def test_a_restore_before_its_boundary_is_written_is_not_repeated(isolated, window):
    """Seen live: the boundary reaches the transcript after SessionStart ran."""
    path = write_transcript(isolated["tmp"] / "t.jsonl", [user("go"), assistant(100000)])
    session_start({"session_id": SESSION, "source": "startup", "transcript_path": str(path)})
    assert "Recorded by the plugin" in compact_start(path)
    assert watch(event(path, hook_event_name="UserPromptSubmit")) is None
    records = [user("go"), assistant(100000), BOUNDARY, user("go on"), assistant(30000)]
    assert watch(event(write_transcript(path, records))) is None
    agent_start({"session_id": SESSION, "agent_id": "sub1"})
    assert "cannot tell whose" in compact_start(path)
    records += [BOUNDARY, assistant(30000)]
    assert "Recorded by the plugin" in watch(event(write_transcript(path, records)))
    assert watch(event(write_transcript(path, records + [assistant(31000)]))) is None


def test_a_restore_and_a_warning_together_stay_under_the_hook_limit(isolated, window):
    from tnc_context.ledger import CONTEXT_MAX_CHARS, LEDGER_MAX_CHARS
    ledger = ledger_path(SESSION, "sub1")
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("y" * LEDGER_MAX_CHARS, encoding="utf-8")
    before = [user("Implement package B2 " + "z" * 500, isSidechain=True)]
    # Short messages pack the record right up to its budget.
    before += [user(f"The coordinator sent a message while you were working:\nfix finding {i}",
                    isSidechain=True, isMeta=True) for i in range(400)]
    before += [assistant(1000 + i, sidechain=True, tools=(("Bash", {"command": "c" * 400}),))
               for i in range(20)]
    assert watch(subagent(isolated, before)) is None
    note = watch(subagent(isolated, before + [SIDE_BOUNDARY, assistant(240000, sidechain=True)]))
    assert "Context is at 240k" in note and "Your ledger is" in note
    assert len(note) <= CONTEXT_MAX_CHARS
