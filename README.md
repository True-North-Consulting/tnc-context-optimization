# TNC Context Optimization

A Claude Code plugin that makes a smaller compaction window safe to use.

Every turn of a Claude Code session re-reads the whole context, so the size of
the context is what a long session costs. A smaller `autoCompactWindow`
compacts earlier and keeps every turn cheaper, but each compaction replaces the
conversation with a lossy summary. This plugin carries the important state
across that compaction:

- **A session ledger.** Claude keeps a short Markdown file per session (goal,
  state, decisions, rejected approaches, open to-dos, pointers). The plugin
  reminds it to write the ledger, warns it before the compaction comes, and
  re-injects the ledger right after the compaction.
- **A verbatim record.** After a compaction the plugin also injects what it
  read from the transcript itself, not a summary: the user's messages (the
  first and the latest), the files edited, the last commands and the git state.
  This works even if Claude never wrote its ledger.
- **A search into the full transcript.** Claude gets a command that finds the
  exact details the summary dropped, such as error text, IDs or a path, on
  either side of every compaction.
- **A status line** that shows the context against the point where Claude Code
  will compact, how long ago the ledger was written, and the model, branch,
  PR, usage limits and prompt-cache state.

## Measured

Over two days of real sessions on one team's codebase, with the plugin and a
300k window, compared with the sessions before on a 1M window:

|                                          | Before | After  |
| ---------------------------------------- | -----: | -----: |
| Cost per turn (Opus 5.5 API list prices) | $0.136 | $0.067 |
| Average context per turn                 | 444k   | 165k   |
| Share of spend at 300k context or more   | 75 %   | 0 %    |
| Sessions that grew past 400k             | 24     | 0      |

Your numbers will differ. The `/tnc-context-optimization:report` command
measures your own sessions before and after (see below).

The same marketplace also offers
[tnc-plan-tracker](https://github.com/True-North-Consulting/tnc-plan-tracker),
a live progress page for multi-package plans.

## Requirements

- Claude Code (the CLI, the desktop app or the IDE extensions). Plugins' hooks
  do not run in claude.ai chat.
- macOS, Linux or Windows, with Python 3.9 or newer on the `PATH` as
  `python3`, `python` or `py`. The plugin uses only the Python standard
  library.
- On Windows, Git for Windows: Claude Code then runs hooks in Git Bash, which
  the plugin's starter script needs. Without it Claude Code falls back to
  PowerShell, where the plugin does not run. WSL works too.
- `git` on the `PATH`, for the branch and commit shown in the record and the
  status line. Without it those parts are left out.

## Install

### From the Claude plugin directory (review pending)

The plugin has been submitted to Anthropic's plugin directory and is waiting
for review. Once it is listed, install it from the **Discover** tab in
`/plugin` in Claude Code, or from [claude.ai/directory](https://claude.ai/directory).
There is no marketplace to add first. Until then, use the marketplace below.

### From the True North Consulting marketplace

```
/plugin marketplace add True-North-Consulting/tnc-context-optimization
/plugin install tnc-context-optimization@true-north-consulting
```

Either way, run the setup command once in a new session:

```
/tnc-context-optimization:setup
```

It checks four things a plugin is not allowed to set for itself and prints
what to add for each one. **It never changes your settings itself.**

1. **The compaction window.** `autoCompactWindow` in `~/.claude/settings.json`.
   300000 halved the cost per turn in the measurement above. Smaller is
   cheaper per turn, larger compacts less often. A value set per model with
   `/autocompact` (in `modelSettings`) is honoured too.
2. **The status line.** `statusLine` pointing at a small launcher the plugin
   keeps in its data directory. The launcher survives plugin updates.
3. **Permission to write the ledger.** A `Read` and an `Edit` rule for the
   plugin's `ledgers/` folder. Without them Claude asks before each ledger
   update.
4. **The compaction rules.** One `@…/rules.md` line for `~/.claude/CLAUDE.md`.
   It tells the compaction what to keep word for word. The rules must come
   from CLAUDE.md: in testing, compaction ignored the same rules when a hook
   injected them.

Settings take effect in sessions started afterwards.

### Upgrading from varis-context-optimization

The plugin was called `varis-context-optimization` before 2.0.0. The new name
gives it a new data directory, so uninstall the old plugin, install
`tnc-context-optimization`, and run `/tnc-context-optimization:setup` again:
the status line, the ledger permissions and the `@…/rules.md` line all name the
data directory. Ledgers of earlier sessions stay in
`~/.claude/plugins/data/varis-context-optimization-true-north-consulting/`.

## Measure your own savings

```
/tnc-context-optimization:report --until 2026-10-01
/tnc-context-optimization:report --since 2026-10-01
```

This reads the current project's transcripts and prints the cost per turn,
the average context, the spend by context size, the peak context and the
number of compactions. Costs are in relative units (input 1, cache read 0.1,
output 5), so the comparison holds whatever the model's price. Sessions are
counted by the day they started. Subagents get a line of their own, under the
sessions that started them, and are not counted in the lines above it.

## Subagents

A subagent gets the same treatment as the main session, with a ledger of its
own. This matters most for long subagents that are resumed with `SendMessage`
for new tasks, and that compact on their own as their context fills.

- When a subagent starts, and each time it is resumed, it is pointed at its
  own ledger, `ledgers/<session>.<agent>.md`. Its Goal is its brief and every
  message sent to it since.
- Its tool batches get the same warning and reminders as the main session.
  The window is the one it inherits, or the lower `autoCompactWindow` its
  definition in `.claude/agents/` sets.
- After it compacts, its ledger, its own record (its brief and messages, files
  edited, last commands) and a search into its own transcript come back with
  its next tool batch. Its first reply after the compaction still has only
  the summary, and if that reply makes no tool call, nothing comes back.

Claude Code runs `SessionStart` inside a subagent too, after its compaction,
but that event names neither the subagent nor its transcript, so it looks
exactly like the main session's. Before 2.1.0 it handed the subagent the main
session's ledger. Now the plugin notes which subagents run (from
`SubagentStart` to `SubagentStop`). While none runs, a compaction can only be
the main session's, and its ledger comes back at once, as before. While one
runs, `SessionStart` injects a short note instead: the main session is told
where its ledger is and to read it before it continues, and a subagent is told
the note is not for it. Whoever compacted then gets its full ledger and record
with its next prompt or tool batch, which do name it.

A subagent that ends without `SubagentStop` (killed, or a crash) stays marked
as running until the session is resumed. Until then the main session gets the
short note after a compaction, and its full ledger one prompt or tool batch
later.

## What it runs, reads and writes

Everything stays on your machine. The plugin makes no network requests.

**Runs.** `sh scripts/run.sh` runs on these events. It starts
`scripts/context_optimization.py` with the first of `python3`, `python` and
`py` that is Python 3.9 or newer:

- `SessionStart`: injects the ledger rule. After a compaction it also injects
  the ledger, the record and the search command, in under 10,000 characters.
- `SubagentStart`: injects the ledger rule with the subagent's own ledger, and
  notes the subagent as running. `SubagentStop` notes it stopped and injects
  nothing.
- `UserPromptSubmit` and `PostToolBatch`: injects a reminder or a warning when
  one is due, and otherwise nothing. After a compaction `SessionStart` could
  not restore, they inject what it would have.
- The status line, if you set it up.

To show the git state it runs `git branch --show-current`,
`git log -1 --format=%h %s` and `git status --porcelain` in the session's
working directory.

**Reads:**

- the session transcript Claude Code passes to the hook, and for a subagent
  its own transcript beside it (`<session>/subagents/agent-<id>.jsonl`);
- when a subagent starts, the frontmatter of the agent definitions in
  `.claude/agents/` in the project and in `~/.claude/agents/`, for its
  `autoCompactWindow`;
- the settings files Claude Code reads for `autoCompactWindow`: managed,
  `.claude/settings.local.json` and `.claude/settings.json` in the project,
  and `~/.claude/settings.json`;
- for `report`, the current project's transcripts in `~/.claude/projects/`;
- for `setup`, `~/.claude/settings.json` and `~/.claude/CLAUDE.md`, to check
  them.

**Writes**, all in the plugin's data directory. Claude Code names it after
the plugin and the marketplace it came from, for example
`~/.claude/plugins/data/tnc-context-optimization-true-north-consulting/`;
`/tnc-context-optimization:setup` prints the exact path:

- `ledgers/<session>.md`, and `ledgers/<session>.<agent>.md` per subagent,
  written by Claude, not by the plugin;
- `state/<session>.json` and `state/<session>.<agent>.json`, which reminders
  have been given;
- `running/<session>.<agent>`, an empty file while that subagent runs;
- `restored/<session>`, how many of the session's compactions it restored
  right away;
- `statusline.sh` and `rules.md`, rewritten at each session start when the
  installed version changed them.

It deletes ledgers and state files that have not been touched for 30 days, at
the start of a new session.

If you set `CLAUDE_CONFIG_DIR`, read every `~/.claude` above as that directory.

**What is in the context.** The ledger and the record hold your own messages
and file paths in plain text, on your disk. Like the rest of the conversation,
what is injected goes to the model. The ledger rule tells Claude never to put
secrets, credentials or personal data in the ledger.

## How the warning is timed

Claude Code compacts a little before the window is full: about 33k tokens
before it. A 1M window compacts at about 967k (documented), and a 300k window
compacted at about 267k (measured over 17 compactions). The plugin warns 40k
before that point, or a quarter of the point on a small window. If the ledger
is still not updated three hook calls later, it warns once more. It also
reminds Claude whenever the conversation has grown by 100k since the ledger
was last written.

The window is read the way Claude Code reads it:
`CLAUDE_CODE_AUTO_COMPACT_WINDOW` first, then the settings files from managed
to user. Within one file, a window set for the current model wins. A window
set with the `--autocompact` launch flag is not visible to hooks. In that case
the warning does not fire, but the ledger reminders still do.

## Development

```
python3 -m pytest -q
claude plugin validate --strict .
```

## License

MIT. See [LICENSE](LICENSE).
