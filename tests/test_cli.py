from __future__ import annotations

import datetime as dt
import importlib.util
import io
import json
import os
import shlex
import shutil
import subprocess
import sys

import pytest

from conftest import ROOT, SESSION, assistant, user, write_settings, write_transcript

from tnc_context import files
from tnc_context.report import report, transcripts_dir

SH = shutil.which("sh")
_spec = importlib.util.spec_from_file_location("context_optimization", ROOT / "scripts" / "context_optimization.py")
entry = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(entry)


def run_main(argv, stdin, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(stdin)))
    code = entry.main(argv)
    return code, capsys.readouterr()


def test_watch_emits_hook_json_for_the_firing_event(isolated, window, monkeypatch, capsys):
    transcript = write_transcript(isolated["tmp"] / "t.jsonl", [assistant(240000)])
    event = {"session_id": SESSION, "transcript_path": str(transcript), "hook_event_name": "UserPromptSubmit"}
    code, out = run_main(["watch"], event, monkeypatch, capsys)
    assert code == 0
    payload = json.loads(out.out)["hookSpecificOutput"]
    assert payload["hookEventName"] == "UserPromptSubmit"
    assert "240k" in payload["additionalContext"]


def test_a_quiet_hook_prints_nothing(isolated, window, monkeypatch, capsys):
    transcript = write_transcript(isolated["tmp"] / "t.jsonl", [assistant(1000)])
    event = {"session_id": SESSION, "transcript_path": str(transcript), "hook_event_name": "PostToolBatch"}
    code, out = run_main(["watch"], event, monkeypatch, capsys)
    assert code == 0 and out.out == ""


def test_unexpected_input_is_reported_on_stderr(isolated, monkeypatch, capsys):
    code, out = run_main(["session-start"], {"session_id": SESSION}, monkeypatch, capsys)
    assert code == 1
    assert out.err.startswith("tnc-context-optimization session-start:")
    assert out.out == ""


def test_the_hooks_call_existing_subcommands():
    hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    assert set(hooks) == {"SessionStart", "SubagentStart", "SubagentStop", "UserPromptSubmit",
                          "PostToolBatch"}
    for entries in hooks.values():
        for item in entries:
            for hook in item["hooks"]:
                command = hook["command"]
                assert command.startswith('sh "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" ')
                assert command.split()[-1] in ("session-start", "agent-start", "agent-stop", "watch")


def test_the_commands_call_existing_subcommands():
    for name, sub in (("setup", "setup"), ("report", "report")):
        text = (ROOT / "commands" / f"{name}.md").read_text(encoding="utf-8")
        assert f'sh "${{CLAUDE_PLUGIN_ROOT}}/scripts/run.sh" {sub}' in text


def test_the_entry_script_runs_on_its_own(isolated):
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / "context_optimization.py"), "--help"],
                            capture_output=True, text=True, check=True)
    assert "session-start" in result.stdout


# --- setup and the stable files -------------------------------------------------


def test_setup_installs_the_files_and_prints_what_is_missing(isolated, monkeypatch, capsys):
    code, out = run_main(["setup", "--project", str(isolated["project"])], {}, monkeypatch, capsys)
    assert code == 0
    data = isolated["data"]
    assert files.launcher_path(data).exists() and files.rules_path(data).exists()
    assert out.out.count("✘") == 4
    assert json.dumps(f"sh {shlex.quote(files.launcher_path(data).as_posix())}") in out.out
    assert f"@{files.rules_path(data)}" in out.out


def test_setup_ticks_what_is_in_place(isolated, monkeypatch, capsys):
    data = isolated["data"]
    write_settings(isolated["home"] / ".claude" / "settings.json", {
        "autoCompactWindow": 300000,
        "statusLine": {"type": "command", "command": f'sh "{files.launcher_path(data)}"'},
        "permissions": {"allow": [f"Read({data}/ledgers/**)", f"Edit({data}/ledgers/**)"]},
    })
    (isolated["home"] / ".claude" / "CLAUDE.md").write_text(f"@{files.rules_path(data)}\n", encoding="utf-8")
    code, out = run_main(["setup"], {}, monkeypatch, capsys)
    assert code == 0 and out.out.count("✔") == 4 and "✘" not in out.out
    assert "compacts at about 267,000" in out.out


def test_setup_writes_no_settings(isolated, monkeypatch, capsys):
    run_main(["setup"], {}, monkeypatch, capsys)
    assert not (isolated["home"] / ".claude" / "settings.json").exists()
    assert not (isolated["home"] / ".claude" / "CLAUDE.md").exists()


def test_setup_needs_a_data_dir(isolated, monkeypatch, capsys):
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA")
    code, out = run_main(["setup"], {}, monkeypatch, capsys)
    assert code == 1 and "--data" in out.err


def test_the_launcher_runs_the_statusline(isolated):
    files.install(isolated["data"])
    status = {"session_id": SESSION, "model": {"display_name": "Opus"}, "workspace": {},
              "context_window": {"context_window_size": 1000000, "current_usage": None}}
    result = subprocess.run([SH, str(files.launcher_path(isolated["data"]))],
                            input=json.dumps(status), capture_output=True, encoding="utf-8", check=True)
    assert "waiting for the first reply" in result.stdout


# --- report -----------------------------------------------------------------------


def session_file(folder, name, started, tokens):
    records = [dict(user("go"), timestamp=started)]
    records += [dict(assistant(t, text=str(i)), timestamp=started) for i, t in enumerate(tokens)]
    return write_transcript(folder / f"{name}.jsonl", records)


def test_the_report_selects_sessions_by_start_date(isolated):
    folder = transcripts_dir(isolated["project"])
    folder.mkdir(parents=True)
    session_file(folder, "before", "2026-10-01T09:00:00Z", [450000, 500000])
    session_file(folder, "after", "2026-10-04T09:00:00Z", [100000, 150000, 200000])
    # A session started before the cut-off stays "before" however late it ran.
    every = report(isolated["project"], None, None)
    after = report(isolated["project"], dt.date(2026, 10, 4), None)
    before = report(isolated["project"], None, dt.date(2026, 10, 4))
    assert every.startswith("2 sessions, 5 turns")
    assert after.startswith("1 sessions, 3 turns") and "average context 150k" in after
    assert before.startswith("1 sessions, 2 turns") and "sessions above 400k: 1" in before


def test_the_report_counts_subagents_apart(isolated):
    folder = transcripts_dir(isolated["project"])
    folder.mkdir(parents=True)
    session_file(folder, "s1", "2026-10-04T09:00:00Z", [100000, 150000])
    subagents = folder / "s1" / "subagents"
    subagents.mkdir(parents=True)
    write_transcript(subagents / "agent-a1.jsonl", [
        dict(assistant(t, sidechain=True, text=str(i)), timestamp="2026-10-04T09:05:00Z")
        for i, t in enumerate((40000, 80000, 120000))])
    text = report(isolated["project"], None, None)
    assert text.startswith("1 sessions, 2 turns")
    assert "subagents (not counted above): 1, 3 turns" in text
    assert "average context 80k, peak max 120k, auto-compactions 0;" in text


def test_the_report_of_an_empty_range(isolated):
    transcripts_dir(isolated["project"]).mkdir(parents=True)
    assert report(isolated["project"], dt.date(2030, 1, 1), None).startswith("no sessions")


def test_setup_and_report_follow_claude_config_dir(isolated, monkeypatch, capsys):
    other = isolated["tmp"] / "profile"
    other.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(other))
    code, out = run_main(["setup"], {}, monkeypatch, capsys)
    assert code == 0 and f"Add to {other / 'settings.json'}" in out.out
    assert transcripts_dir(isolated["project"]).parent == other / "projects"


def test_setup_accepts_the_paths_written_out_or_with_a_tilde(isolated, monkeypatch, capsys):
    data = isolated["home"] / ".claude" / "plugins" / "data" / "x"
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(data))
    write_settings(isolated["home"] / ".claude" / "settings.json", {"permissions": {"allow": [
        f"Read({data}/ledgers/**)", "Edit(~/.claude/plugins/data/x/ledgers/**)"]}})
    (isolated["home"] / ".claude" / "CLAUDE.md").write_text("@~/.claude/plugins/data/x/rules.md\n",
                                                           encoding="utf-8")
    code, out = run_main(["setup"], {}, monkeypatch, capsys)
    assert "✔ ledger reads and writes allowed" in out.out
    assert "✔ compact rules imported" in out.out


# --- starting Python through run.sh, as the hooks do ------------------------------


def test_each_hook_command_runs_as_written(isolated):
    """The command lines from hooks.json, through sh, on every OS CI runs."""
    hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    transcript = write_transcript(isolated["tmp"] / "t.jsonl", [user("go"), assistant(1000)])
    for name, entries in hooks.items():
        command = entries[0]["hooks"][0]["command"].replace("${CLAUDE_PLUGIN_ROOT}", ROOT.as_posix())
        event = {"session_id": SESSION, "transcript_path": str(transcript), "hook_event_name": name,
                 "source": "startup", "cwd": str(isolated["project"])}
        if name in ("SubagentStart", "SubagentStop"):
            event.update(agent_id="a1b2c3", agent_type="general-purpose")
        result = subprocess.run([SH, "-c", command], input=json.dumps(event), capture_output=True,
                                encoding="utf-8", check=True)
        if name in ("SessionStart", "SubagentStart"):
            assert json.loads(result.stdout)["hookSpecificOutput"]["hookEventName"] == name
        if name == "SubagentStop":
            assert result.stdout == ""  # context there would keep the subagent running


@pytest.mark.skipif(os.name == "nt", reason="builds a PATH of symlinks")
def test_run_sh_passes_over_a_python3_that_is_not_python(isolated):
    """Like the Microsoft Store stub: `python3` exists but is no Python."""
    bin_dir = isolated["tmp"] / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "python3"
    stub.write_text("#!/bin/sh\necho 'Python was not found' >&2\nexit 9009\n", encoding="utf-8")
    stub.chmod(0o755)
    (bin_dir / "python").symlink_to(sys.executable)
    result = subprocess.run([SH, str(ROOT / "scripts" / "run.sh"), "--help"], capture_output=True,
                            encoding="utf-8", env={**os.environ, "PATH": str(bin_dir)})
    assert result.returncode == 0 and "session-start" in result.stdout


@pytest.mark.skipif(os.name == "nt", reason="needs a PATH without any Python")
def test_run_sh_without_python_says_so(isolated):
    empty = isolated["tmp"] / "empty"
    empty.mkdir()
    result = subprocess.run([SH, str(ROOT / "scripts" / "run.sh"), "--help"], capture_output=True,
                            encoding="utf-8", env={**os.environ, "PATH": str(empty)})
    assert result.returncode == 1 and "no Python 3.9 or newer found" in result.stderr
