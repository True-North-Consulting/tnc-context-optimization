"""The ledger reminders: once before a compaction, again if that one is ignored,
and whenever the conversation has grown a lot since the ledger was written.
A subagent gets its own, and its ledger back after its compaction."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional

from .ledger import after_compaction, age_text, kind, ledger_path, read_state, restored_count, write_state
from .settings import REARM_BELOW, compact_window, compaction_point, warn_point
from .transcript import agent_transcript, boundaries_since, latest_call

# Context growth after which Claude is reminded of a ledger it has not
# touched — again after each further step of this size.
STALE_GROWTH = 100_000
# Hook calls (prompts and tool batches) after the warning without a ledger
# write, before the warning is repeated once, more firmly.
ESCALATE_AFTER = 3


def watch(event: dict) -> Optional[str]:
    """The session's reminders, or a subagent's: each has its own ledger and state."""
    session, agent = event["session_id"], event.get("agent_id")
    subagent = agent is not None
    transcript = agent_transcript(event) if subagent else Path(event["transcript_path"])
    if not transcript.exists():
        return None  # a new session's first prompt comes before its transcript
    ledger = ledger_path(session, agent)
    state = read_state(session, agent)
    before = dict(state)
    compacted = _compacted(state, session, transcript, subagent)
    call = latest_call(transcript, sidechain=subagent)
    note = None if call is None else _note(state, call, ledger, event, subagent)
    if state != before:
        write_state(session, agent, state)
    if not compacted:
        return note
    restored = after_compaction(ledger, transcript, event.get("cwd"), time.time(), subagent,
                                reserve=len(note) + 2 if note else 0)
    return "\n\n".join(part for part in (restored, note) if part)


def _note(state: dict, call, ledger: Path, event: dict, subagent: bool) -> Optional[str]:
    """The warning or reminder due at this context size, if any; updates `state`."""
    tokens = call.tokens
    written = ledger.stat().st_mtime if ledger.exists() else None
    note = None
    window = compact_window(os.environ.get("CLAUDE_PROJECT_DIR") or event.get("cwd"), call.model,
                            state["own_window"])
    if window is not None:
        point = compaction_point(window)
        if state["warned"] and tokens < REARM_BELOW * point:
            state.update(warned=False, escalated=False, calls_since_warning=0)
        elif not state["warned"] and tokens >= warn_point(point):
            state.update(warned=True, escalated=False, calls_since_warning=0,
                         warned_ledger=written, nudged_tokens=tokens)
            note = (
                f"Context is at {tokens // 1000}k; auto-compaction comes at about "
                f"{point // 1000}k. Bring your {kind(subagent)} ({ledger}) up to date now, "
                f"before continuing."
            )
        elif state["warned"] and not state["escalated"]:
            if written != state["warned_ledger"]:
                state["escalated"] = True  # done: the ledger was written after the warning
            else:
                state["calls_since_warning"] += 1
                if state["calls_since_warning"] >= ESCALATE_AFTER:
                    state["escalated"] = True
                    note = (
                        f"Your {kind(subagent)} ({ledger}) is still not updated since the warning, "
                        f"and auto-compaction is about {max(0, point - tokens) // 1000}k away. "
                        f"Update it with your next tool call; what is not in it may be lost."
                    )
    if (written != state["ledger_written"] or state["ledger_tokens"] is None
            or tokens < state["ledger_tokens"]):
        # The ledger was just written, this is the first look, or a compaction
        # shrank the context (and re-injected the ledger): count from here.
        state.update(ledger_written=written, ledger_tokens=tokens, nudged_tokens=tokens)
    elif note is None and tokens - state["nudged_tokens"] >= STALE_GROWTH:
        state["nudged_tokens"] = tokens
        grown = (tokens - state["ledger_tokens"]) // 1000
        if written is None:
            note = (
                f"The {'task' if subagent else 'session'} has grown by {grown}k without a "
                f"{kind(subagent)}. Unless it only answers a quick question, write one now "
                f"at {ledger}."
            )
        else:
            note = (
                f"Your {kind(subagent)} ({ledger}) was last written {age_text(time.time() - written)} "
                f"ago, {grown}k tokens of conversation back. If the goal, the state, a decision, "
                f"a rejected approach or the next step has changed since, update it with a small "
                f"edit; if nothing has, carry on."
            )
    return note


def _compacted(state: dict, session: str, transcript: Path, subagent: bool) -> bool:
    """Whether this conversation compacted since watch last looked, without
    SessionStart having restored it.

    SessionStart never restores a subagent: it cannot name it. It restores the
    session while no subagent runs and counts those restores; the session's
    compactions beyond that count are the ones it left to watch.
    """
    offset = state["transcript_bytes"]
    if offset is None:  # first look: count from here
        state.update(transcript_bytes=transcript.stat().st_size,
                     restored_base=0 if subagent else restored_count(session))
        return False
    found, state["transcript_bytes"] = boundaries_since(transcript, offset, subagent)
    if subagent:
        return found > 0
    state["compactions"] += found
    left = state["compactions"] - (restored_count(session) - state["restored_base"])
    if left <= 0:
        return False  # restored at SessionStart, or its boundary is not written yet
    state["compactions"] -= left
    return True
