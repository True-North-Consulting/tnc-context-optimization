from __future__ import annotations

import os
import subprocess

import pytest
from conftest import BOUNDARY, SESSION, assistant, user, write_transcript

from tnc_context import HookError, files
from tnc_context.ledger import (CONTEXT_MAX_CHARS, LEDGER_MAX_CHARS, RETENTION_DAYS, agent_start,
                                agent_stop, ledger_path, recorded, session_start, state_path)
from tnc_context.transcript import Activity

NOW = 1_800_000_000.0


def start_event(isolated, source: str, records: list = (), cwd=None) -> dict:
    """A SessionStart event; a compaction's transcript ends at its boundary, as Claude Code
    writes it before the hook runs."""
    records = list(records) or [user("hello")]
    if source == "compact":
        records.append(BOUNDARY)
    transcript = write_transcript(isolated["tmp"] / "t.jsonl", records)
    return {"session_id": SESSION, "source": source, "transcript_path": str(transcript),
            "cwd": str(cwd) if cwd else None}


@pytest.mark.parametrize("source", ["startup", "resume", "clear"])
def test_session_start_names_the_ledger_and_its_headings(isolated, source):
    text = session_start(start_event(isolated, source), now=NOW)
    assert str(ledger_path(SESSION)) in text
    assert "Goal" in text and "Pointers" in text
    assert "Recorded by the plugin" not in text


def test_after_compaction_the_ledger_record_and_search_come_back(isolated):
    ledger = ledger_path(SESSION)
    ledger.parent.mkdir(parents=True)
    ledger.write_text("## Goal\nShip the export fix\n", encoding="utf-8")
    event = start_event(isolated, "compact", [
        user("Bitte den Export reparieren"),
        assistant(1000, tools=(("Edit", {"file_path": "/p/export.py"}), ("Bash", {"command": "pytest -q"}))),
    ])
    text = session_start(event, now=NOW)
    assert "Ship the export fix" in text
    assert "Recorded by the plugin from the transcript (verbatim, not a summary):" in text
    assert "“Bitte den Export reparieren”" in text
    assert "Files edited, newest first: /p/export.py" in text
    assert "$ pytest -q" in text
    assert "/scripts/run.sh grep " in text
    assert len(text) <= CONTEXT_MAX_CHARS


def test_after_compaction_without_a_ledger_it_says_so(isolated):
    text = session_start(start_event(isolated, "compact"), now=NOW)
    assert "No session ledger was written before this compaction" in text


def test_an_oversized_ledger_is_pointed_to_not_injected(isolated):
    ledger = ledger_path(SESSION)
    ledger.parent.mkdir(parents=True)
    ledger.write_text("x" * (LEDGER_MAX_CHARS + 1), encoding="utf-8")
    text = session_start(start_event(isolated, "compact"), now=NOW)
    assert "Read it now" in text and "x" * 100 not in text


def test_the_whole_injection_stays_under_the_hook_limit(isolated):
    ledger = ledger_path(SESSION)
    ledger.parent.mkdir(parents=True)
    ledger.write_text("y" * LEDGER_MAX_CHARS, encoding="utf-8")
    many = [user(f"prompt {i} " + "z" * 900) for i in range(40)]
    many += [assistant(1000 + i, tools=(("Edit", {"file_path": f"/p/f{i}.py"}),
                                        ("Bash", {"command": "c" * 400}))) for i in range(30)]
    text = session_start(start_event(isolated, "compact", many), now=NOW)
    assert len(text) <= CONTEXT_MAX_CHARS
    assert "prompt 0" in text  # the opening ask is always kept


def test_the_record_keeps_the_first_and_latest_prompts():
    done = Activity([f"ask {i}" for i in range(1, 51)], [], [])
    text = recorded(done, None, 600)
    assert "“ask 1”" in text and "“ask 50”" in text and "“ask 2”" not in text
    assert "omitted — grep for them" in text
    assert len(text) <= 600


def test_the_record_shows_the_git_state(isolated):
    repo = isolated["tmp"] / "repo"
    repo.mkdir()
    for args in (["init", "-q", "-b", "feat/x"], ["-c", "user.name=t", "-c", "user.email=t@example.com",
                                                 "commit", "-q", "--allow-empty", "-m", "first"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True)
    (repo / "new.txt").write_text("x", encoding="utf-8")
    text = recorded(Activity([], [str(repo / "new.txt")], []), str(repo), 2000)
    assert "branch feat/x" in text and "first" in text and "1 uncommitted" in text
    assert "Files edited, newest first: new.txt" in text


def test_startup_prunes_old_ledgers_and_state(isolated):
    old, fresh = ledger_path("old-session"), ledger_path("fresh-session")
    old.parent.mkdir(parents=True)
    old_state = state_path("old-session")
    old_state.parent.mkdir(parents=True)
    for path in (old, fresh, old_state):
        path.write_text("x", encoding="utf-8")
    stale = NOW - (RETENTION_DAYS + 1) * 86400
    os.utime(old, (stale, stale))
    os.utime(old_state, (stale, stale))
    os.utime(fresh, (NOW, NOW))
    session_start(start_event(isolated, "resume"), now=NOW)
    assert old.exists()  # only a fresh start prunes
    session_start(start_event(isolated, "startup"), now=NOW)
    assert not old.exists() and not old_state.exists() and fresh.exists()


def test_session_start_writes_the_launcher_and_rules(isolated):
    data = isolated["data"]
    session_start(start_event(isolated, "startup"), now=NOW)
    assert files.launcher_path(data).exists()
    files.rules_path(data).write_text("old rules", encoding="utf-8")
    session_start(start_event(isolated, "resume"), now=NOW)
    assert files.rules_path(data).read_text(encoding="utf-8") == files.RULES.read_text(encoding="utf-8")


@pytest.mark.parametrize("session_id", ["../escape", "a/b", "", "x y"])
def test_session_ids_that_could_leave_the_data_dir_are_refused(isolated, session_id):
    with pytest.raises(HookError):
        ledger_path(session_id)


def test_without_plugin_data_the_hook_fails_loudly(isolated, monkeypatch):
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA")
    with pytest.raises(HookError, match="CLAUDE_PLUGIN_DATA"):
        session_start(start_event(isolated, "startup"), now=NOW)


def test_while_a_subagent_runs_a_compaction_is_left_to_watch(isolated):
    """Its SessionStart has no agent_id and names the parent's transcript."""
    ledger = ledger_path(SESSION)
    ledger.parent.mkdir(parents=True)
    ledger.write_text("## Goal\nThe parent's goal\n", encoding="utf-8")
    agent_start({"session_id": SESSION, "agent_id": "a1"})
    unsure = session_start(start_event(isolated, "compact"), now=NOW)
    assert "If you are a subagent, this note is not for you" in unsure
    assert "The parent's goal" not in unsure
    agent_stop({"session_id": SESSION, "agent_id": "a1"})
    assert "The parent's goal" in session_start(start_event(isolated, "compact"), now=NOW)


def test_a_subagent_from_another_session_does_not_count(isolated):
    agent_start({"session_id": "other-session", "agent_id": "a1"})
    assert "Recorded by the plugin" in session_start(start_event(isolated, "compact"), now=NOW)


def test_stopping_a_subagent_never_started_here_is_fine(isolated):
    agent_stop({"session_id": SESSION, "agent_id": "a1"})


def test_a_subagent_gets_a_ledger_of_its_own(isolated):
    text = agent_start({"session_id": SESSION, "agent_id": "a1b2c3", "agent_type": "Explore",
                        "transcript_path": str(isolated["tmp"] / "t.jsonl")})
    ledger = ledger_path(SESSION, "a1b2c3")
    assert ledger.name == f"{SESSION}.a1b2c3.md" and ledger != ledger_path(SESSION)
    assert text.startswith(f"Your ledger is {ledger}.")
    assert "Goal (your brief and every message sent to you since, quoted word for word)" in text
    assert "A task that only answers a quick question needs no ledger." in text
    assert ledger.parent.is_dir()


def test_the_session_rule_is_unchanged(isolated):
    text = session_start(start_event(isolated, "startup"), now=NOW)
    assert text.startswith(f"Your session ledger is {ledger_path(SESSION)}.")
    assert "Goal (the user's requests, quoted word for word)" in text
    assert "A session that only answers a quick question needs no ledger." in text


def test_after_a_subagents_compaction_its_own_record_comes_back(isolated):
    from tnc_context.ledger import after_compaction
    ledger = ledger_path(SESSION, "a1b2c3")
    ledger.parent.mkdir(parents=True)
    ledger.write_text("## Goal\nPackage B2\n", encoding="utf-8")
    transcript = write_transcript(isolated["tmp"] / "agent-a1b2c3.jsonl", [
        user("Implement package B2", isSidechain=True),
        assistant(1000, sidechain=True, tools=(("Edit", {"file_path": "/p/b2.py"}),)),
        user("The coordinator sent a message while you were working:\nAlso fix the review findings",
             isSidechain=True, isMeta=True),
        BOUNDARY,
    ])
    text = after_compaction(ledger, transcript, None, NOW, agent=True)
    assert "Your ledger (" in text and "Package B2" in text
    assert "Your brief and the messages sent to you (2; the first and the latest):" in text
    assert "“Also fix the review findings”" in text
    assert "Files edited, newest first: /p/b2.py" in text
    assert "/scripts/run.sh grep " in text and "agent-a1b2c3.jsonl" in text
    assert "Your ledger is" in text and len(text) <= CONTEXT_MAX_CHARS


@pytest.mark.parametrize("agent_id", ["../x", "a.b", ""])
def test_agent_ids_that_could_leave_the_data_dir_are_refused(isolated, agent_id):
    with pytest.raises(HookError):
        agent_start({"session_id": SESSION, "agent_id": agent_id})


def test_state_is_written_whole(isolated):
    from tnc_context.ledger import read_state, write_state
    write_state(SESSION, "a1", dict(read_state(SESSION, "a1"), pending=True))
    assert read_state(SESSION, "a1")["pending"] is True
    assert [p.name for p in state_path(SESSION).parent.iterdir()] == [f"{SESSION}.a1.json"]
