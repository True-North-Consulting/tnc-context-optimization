#!/usr/bin/env python3
"""TNC Context Optimization: auto-compaction that keeps what matters.

Claude Code compacts on its own once the context nears the
`autoCompactWindow`; the summary it writes is lossy. This keeps the important
state outside the conversation, where compaction cannot touch it:

  session-start  SessionStart hook. Points Claude at its session ledger and,
                 right after a compaction, re-injects that ledger, the
                 plugin's own verbatim record of the session (the user's
                 messages, files edited, last commands, git state) and the
                 command that searches the full pre-compaction transcript.
  agent-start    SubagentStart hook, on a spawn and on every resume: points
                 the subagent at a ledger of its own and marks it running.
  agent-stop     SubagentStop hook: marks it stopped.
  watch          UserPromptSubmit / PostToolBatch hook. Before the compaction
                 point asks Claude to bring the ledger up to date, repeats it
                 once if ignored, and reminds it whenever the conversation has
                 grown by 100k since the ledger was last written. After a
                 compaction SessionStart left alone (one it could not tell
                 apart from a running subagent's), it re-injects the ledger.
  statusline     statusLine command: the context against the compaction point
                 and the session's ledger, then model, branch, PR, usage
                 limits and prompt cache.
  grep           Searches a session transcript for the exact details a
                 compaction summarised away.
  report         Where a project's sessions spend their tokens, by session
                 start date — to compare before and after.
  setup          Writes the stable status-line launcher and rules copy and
                 prints the settings a plugin cannot set for itself.

Standard library only, Python 3.9+. Nothing leaves the machine.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tnc_context import PLUGIN_NAME, HookError  # noqa: E402
from tnc_context.ledger import agent_start, agent_stop, session_start  # noqa: E402
from tnc_context.report import report  # noqa: E402
from tnc_context.setup import setup  # noqa: E402
from tnc_context.statusline import statusline  # noqa: E402
from tnc_context.transcript import grep  # noqa: E402
from tnc_context.watch import watch  # noqa: E402


def hook_output(event_name: str, context: Optional[str]) -> None:
    if context:
        json.dump(
            {"hookSpecificOutput": {"hookEventName": event_name, "additionalContext": context}},
            sys.stdout,
        )


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("session-start")
    commands.add_parser("agent-start")
    commands.add_parser("agent-stop")
    commands.add_parser("watch")
    commands.add_parser("statusline")
    grep_parser = commands.add_parser("grep")
    grep_parser.add_argument("transcript", type=Path)
    grep_parser.add_argument("pattern")
    grep_parser.add_argument("--max", type=int, default=20, dest="limit")
    grep_parser.add_argument("--width", type=int, default=160)
    report_parser = commands.add_parser("report")
    report_parser.add_argument("--project", type=Path, default=Path.cwd())
    report_parser.add_argument("--since", type=dt.date.fromisoformat, help="sessions started on or after")
    report_parser.add_argument("--until", type=dt.date.fromisoformat, help="sessions started before")
    setup_parser = commands.add_parser("setup")
    setup_parser.add_argument("--data", default=os.environ.get("CLAUDE_PLUGIN_DATA"),
                              help="the plugin's data directory (default: $CLAUDE_PLUGIN_DATA)")
    setup_parser.add_argument("--project", default=os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    return root


def main(argv: Optional[list] = None) -> int:
    # Claude Code exchanges UTF-8; Windows would otherwise use its ANSI code page.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    try:
        if args.command == "grep":
            print(grep(args.transcript, args.pattern, args.limit, args.width))
        elif args.command == "report":
            print(report(args.project, args.since, args.until))
        elif args.command == "setup":
            if not args.data:
                raise HookError("no data directory: pass --data or run it as the plugin's /setup command")
            print(setup(Path(args.data), args.project))
        elif args.command == "statusline":
            print(statusline(json.load(sys.stdin)))
        elif args.command == "session-start":
            hook_output("SessionStart", session_start(json.load(sys.stdin)))
        elif args.command == "agent-start":
            hook_output("SubagentStart", agent_start(json.load(sys.stdin)))
        elif args.command == "agent-stop":
            agent_stop(json.load(sys.stdin))
        else:
            event = json.load(sys.stdin)
            hook_output(event["hook_event_name"], watch(event))
    except (HookError, KeyError, OSError, ValueError) as error:
        print(f"{PLUGIN_NAME} {args.command}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
