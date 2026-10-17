"""The session ledger: where it lives, the rule for Claude, and what comes back
after a compaction."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Optional

from . import HookError, files
from .settings import agent_window
from .transcript import Activity, activity

# What a SessionStart hook can inject is capped at 10,000 characters.
CONTEXT_MAX_CHARS = 9500
# Claude's ledger and the plugin's own record share that budget.
LEDGER_MAX_CHARS = 5500
PROMPT_MAX_CHARS = 600
EDITED_SHOWN = 12
COMMANDS_SHOWN = 5
COMMAND_MAX_CHARS = 140
# Ledgers and state files of sessions untouched this long are deleted.
RETENTION_DAYS = 30

# After a compaction SessionStart could not attribute: it reaches whoever
# compacted, the main session or one of its running subagents.
UNATTRIBUTED = """\
Auto-compaction has just replaced the earlier conversation with a summary. A \
subagent is running, so the plugin cannot tell whose compaction this was. If \
you are the main session: your session ledger is {ledger}. Read it before you \
continue; the plugin's record follows with your next prompt or tool call. If \
you are a subagent, this note is not for you: your own ledger comes back with \
your next tool call."""

LEDGER_RULE = """\
Your {kind} is {ledger}. It is your working memory across \
auto-compaction, which can happen at any moment: create it at the first \
milestone, then update it with a small edit at each later one — a decision of \
the user's, an approach rejected, a commit or PR, a gotcha found. Use these \
`## ` headings, in this order: Goal ({goal}, quoted word for \
word) · State (branch, commits, what is verified and how) · Decisions (with \
why) · Rejected (with why) · Open (to-dos in order, next step first) · \
Pointers (path:line, PR numbers, IDs, exact error text). Stay under {limit:,} \
characters; never put secrets, credentials or personal data in it. A {work} \
that only answers a quick question needs no ledger."""


def data_dir() -> Path:
    value = os.environ.get("CLAUDE_PLUGIN_DATA")
    if not value:
        raise HookError("CLAUDE_PLUGIN_DATA is unset; run this as the plugin's hook")
    return Path(value)


def conversation(session_id: str, agent_id: Optional[str] = None) -> str:
    """The file key of one conversation: the session, or one subagent in it."""
    key = checked(session_id)
    return key if agent_id is None else f"{key}.{checked(agent_id)}"


def ledger_path(session_id: str, agent_id: Optional[str] = None) -> Path:
    return data_dir() / "ledgers" / f"{conversation(session_id, agent_id)}.md"


def state_path(session_id: str, agent_id: Optional[str] = None) -> Path:
    return data_dir() / "state" / f"{conversation(session_id, agent_id)}.json"


def running_path(session_id: str, agent_id: str) -> Path:
    """Present while the subagent runs: from SubagentStart to SubagentStop."""
    return data_dir() / "running" / conversation(session_id, agent_id)


def restored_path(session_id: str) -> Path:
    """How many compactions SessionStart restored the session at; only it writes this."""
    return data_dir() / "restored" / checked(session_id)


def restored_count(session_id: str) -> int:
    try:
        return int(restored_path(session_id).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return 0


def read_state(session_id: str, agent_id: Optional[str] = None) -> dict:
    state = {"warned": False, "escalated": False, "calls_since_warning": 0, "warned_ledger": None,
             "ledger_written": None, "ledger_tokens": None, "nudged_tokens": None,
             "transcript_bytes": None, "own_window": None,
             # the session's compactions found since transcript_bytes was first set, less
             # those watch restored, and SessionStart's count of restores at that time
             "compactions": 0, "restored_base": 0}
    try:
        state.update(json.loads(state_path(session_id, agent_id).read_text(encoding="utf-8")))
    except FileNotFoundError:
        pass
    return state


def write_state(session_id: str, agent_id: Optional[str], state: dict) -> None:
    _write_whole(state_path(session_id, agent_id), json.dumps(state))


def _write_whole(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    partial.write_text(text, encoding="utf-8")
    os.replace(partial, path)  # a hook reading it never sees half a file


def kind(agent: bool) -> str:
    """What Claude is told its ledger is: a subagent's is its own, not the session's."""
    return "ledger" if agent else "session ledger"


def checked(session_id: str) -> str:
    """A session id safe to use as a file name inside the data dir."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", session_id or ""):
        raise HookError(f"unexpected session_id: {session_id!r}")
    return session_id


def rule(ledger: Path, agent: bool = False) -> str:
    if agent:
        goal, work = "your brief and every message sent to you since", "task"
    else:
        goal, work = "the user's requests", "session"
    return LEDGER_RULE.format(kind=kind(agent), ledger=ledger, goal=goal, work=work,
                              limit=LEDGER_MAX_CHARS)


def grep_command(transcript: str) -> str:
    return files.command("grep", transcript, "<regex>")


def session_start(event: dict, now: Optional[float] = None) -> Optional[str]:
    now = time.time() if now is None else now
    source = event["source"]
    ledger = ledger_path(event["session_id"])
    ledger.parent.mkdir(parents=True, exist_ok=True)
    files.install(data_dir())
    if source == "startup":
        prune(now)
    session = event["session_id"]
    transcript = Path(event["transcript_path"])
    running = list((data_dir() / "running").glob(f"{checked(session)}.*"))
    if source != "compact":
        if source == "resume":
            for mark in running:  # its subagents ended with the previous process
                mark.unlink()
        state = read_state(session)
        if state["transcript_bytes"] is None:  # watch looks for compactions from here on
            state.update(transcript_bytes=transcript.stat().st_size if transcript.exists() else 0,
                         restored_base=restored_count(session))
            write_state(session, None, state)
        return rule(ledger)
    # A subagent's SessionStart carries no agent_id and names the parent's
    # transcript. While none runs this compaction is the session's. While one
    # runs, whoever compacted gets its ledger with its next prompt or tool
    # batch (watch): the session for each of its compactions beyond the ones
    # restored here, whose boundary may reach the transcript only after this hook.
    if running:
        return UNATTRIBUTED.format(ledger=ledger)
    _write_whole(restored_path(session), str(restored_count(session) + 1))
    return after_compaction(ledger, transcript, event.get("cwd"), now)


def agent_start(event: dict) -> str:
    """SubagentStart, on a spawn and on every resume: the subagent's own ledger."""
    session, agent = event["session_id"], event["agent_id"]
    ledger = ledger_path(session, agent)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    running = running_path(session, agent)
    running.parent.mkdir(parents=True, exist_ok=True)
    running.touch()
    if event.get("agent_type"):
        # Read once here: a broken definition fails this hook, not every batch.
        state = read_state(session, agent)
        state["own_window"] = agent_window(os.environ.get("CLAUDE_PROJECT_DIR") or event.get("cwd"),
                                           event["agent_type"])
        write_state(session, agent, state)
    return rule(ledger, agent=True)


def agent_stop(event: dict) -> None:
    """SubagentStop: no output, which there would keep the subagent running."""
    try:
        running_path(event["session_id"], event["agent_id"]).unlink()
    except FileNotFoundError:
        pass  # started before this version, or already stopped


def after_compaction(ledger: Path, transcript: Path, cwd: Optional[str], now: float,
                     agent: bool = False, reserve: int = 0) -> str:
    """The ledger, the plugin's own record and the search, for right after a
    compaction, leaving `reserve` characters of the hook's cap to a note."""
    head = ["Auto-compaction has just replaced the earlier conversation with a summary."]
    if ledger.exists():
        text = ledger.read_text(encoding="utf-8")
        age = age_text(now - ledger.stat().st_mtime)
        if len(text) > LEDGER_MAX_CHARS:
            head.append(
                f"Your {kind(agent)} ({ledger}, updated {age} ago) is {len(text):,} characters, "
                f"over the {LEDGER_MAX_CHARS:,} that fit here: Read it now, then tighten it."
            )
        else:
            head.append(
                f"Your {kind(agent)} ({ledger}, updated {age} ago) follows. Where it and the "
                f"summary disagree, the more recent information wins.\n\n{text.strip()}"
            )
    else:
        head.append(f"No {kind(agent)} was written before this compaction ({ledger}).")
    tail = [
        "Exact details the summary dropped — commands, error text, the user's words, IDs — "
        "are still in the full transcript. Search it instead of guessing:\n"
        f"  {grep_command(str(transcript))}",
        rule(ledger, agent),
    ]
    budget = CONTEXT_MAX_CHARS - reserve - len("\n\n".join(head + tail)) - 2
    record = recorded(activity(transcript, sidechain=agent), cwd, budget, agent)
    return "\n\n".join(head + ([record] if record else []) + tail)


def recorded(done: Activity, cwd: Optional[str], budget: int, agent: bool = False) -> str:
    """The plugin's own record of the session, verbatim, within `budget` chars.

    It does not depend on Claude having kept its ledger: the user's words, the
    files edited, the last commands and the git state come from the transcript.
    """
    lines = ["Recorded by the plugin from the transcript (verbatim, not a summary):"]
    git = git_state(cwd)
    if git:
        lines.append(f"Git: {git}")
    if done.edited:
        shown = [_relative(p, cwd) for p in reversed(done.edited[-EDITED_SHOWN:])]
        more = len(done.edited) - len(shown)
        lines.append("Files edited, newest first: " + ", ".join(shown)
                     + (f" (+{more} more)" if more > 0 else ""))
    if done.commands:
        lines.append("Last commands:")
        lines += [f"  $ {_clip(c.splitlines()[0], COMMAND_MAX_CHARS)}"
                  for c in done.commands[-COMMANDS_SHOWN:]]
    fixed = "\n".join(lines)
    room = budget - len(fixed) - 1
    prompts = _prompts(done.prompts, room, agent)
    text = "\n".join([lines[0], prompts] + lines[1:]) if prompts else fixed
    return text if len(text) <= budget else ""


def _prompts(prompts: list, room: int, agent: bool = False) -> str:
    """The first prompt (the opening ask) and as many of the latest as fit."""
    whose = "Your brief and the messages sent to you" if agent else "The user's messages"
    if not prompts:
        return ""
    numbered = [f"  [{i}] “{_clip(p, PROMPT_MAX_CHARS)}”" for i, p in enumerate(prompts, 1)]

    def block(kept: list) -> str:
        omitted = len(numbered) - len(kept)
        title = (f"{whose} ({len(prompts)}; the first and the latest"
                 + (f", {omitted} in between omitted — grep for them):" if omitted else "):"))
        return "\n".join([title] + kept)

    kept = [numbered[0]]
    for line in reversed(numbered[1:]):
        if len(block([kept[0], line] + kept[1:])) > room:
            break
        kept.insert(1, line)
    text = block(kept)
    return text if len(text) <= room else ""


def git_state(cwd: Optional[str]) -> Optional[str]:
    """Branch, HEAD and uncommitted count of the session's working dir."""
    if not cwd:
        return None
    branch = _git(cwd, "branch", "--show-current")
    head = _git(cwd, "log", "-1", "--format=%h %s")
    if head is None:
        return None
    status = _git(cwd, "status", "--porcelain") or ""
    dirty = len([line for line in status.splitlines() if line.strip()])
    parts = [f"branch {branch or '(detached)'}", f"HEAD {_clip(head, 90)}"]
    parts.append(f"{dirty} uncommitted" if dirty else "clean")
    return f"{cwd} · " + " · ".join(parts)


def _git(cwd: str, *args: str) -> Optional[str]:
    try:
        result = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True,
                                timeout=2, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def prune(now: float) -> None:
    cutoff = now - RETENTION_DAYS * 86400
    for folder in ("ledgers", "state", "running", "restored"):
        for path in (data_dir() / folder).glob("*"):
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
            except FileNotFoundError:
                continue  # another session pruned it first


def age_text(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60} min"


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _relative(path: str, cwd: Optional[str]) -> str:
    if not cwd:
        return path
    try:
        return str(Path(path).relative_to(cwd))
    except ValueError:
        return path  # outside the working dir
