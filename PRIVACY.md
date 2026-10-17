# Privacy

TNC Context Optimization is published by True North Consulting. It runs
entirely on your machine.

**What we receive: nothing.** The plugin makes no network requests and has no
server. We collect no data, no telemetry and no usage statistics.

**What stays on your machine.** To carry your session across a compaction,
the plugin reads the session transcript Claude Code passes to it and keeps
files in its data directory. Claude Code names that directory after the
plugin and the marketplace it came from, for example
`~/.claude/plugins/data/tnc-context-optimization-true-north-consulting/`;
`/tnc-context-optimization:setup` prints the exact path. It holds
the ledger Claude writes and a small state file per session and per subagent.
These hold your
own messages and file paths in plain text, so they can contain whatever you
typed, including names or email addresses. Files untouched for 30 days are
deleted at the start of a new session; you can delete the directory at any
time. The full list of what it runs, reads and writes is in the
[README](README.md#what-it-runs-reads-and-writes).

**What goes to the model.** After a compaction the plugin injects the ledger
and a record of your messages into the conversation. Like the rest of the
conversation, that text goes to the model under the terms of your Claude
account. The plugin sends it nowhere else.

**Contact.** Questions or concerns: open an issue at
https://github.com/True-North-Consulting/tnc-context-optimization/issues.
