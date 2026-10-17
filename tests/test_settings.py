from __future__ import annotations

from pathlib import Path

import pytest
from conftest import write_settings

from tnc_context import HookError
from tnc_context.settings import agent_window, compact_window, compaction_point, warn_point


def test_window_unset_everywhere_is_none(isolated):
    assert compact_window(str(isolated["project"])) is None


def test_window_precedence_env_managed_local_project_user(isolated, monkeypatch):
    project = str(isolated["project"])
    write_settings(isolated["home"] / ".claude" / "settings.json", {"autoCompactWindow": 100000})
    assert compact_window(project) == 100000
    write_settings(isolated["project"] / ".claude" / "settings.json", {"autoCompactWindow": 200000})
    assert compact_window(project) == 200000
    write_settings(isolated["project"] / ".claude" / "settings.local.json", {"autoCompactWindow": 250000})
    assert compact_window(project) == 250000
    write_settings(isolated["managed"], {"autoCompactWindow": 350000})
    assert compact_window(project) == 350000
    monkeypatch.setenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "400000")
    assert compact_window(project) == 400000


def test_files_without_the_key_are_skipped(isolated):
    write_settings(isolated["project"] / ".claude" / "settings.json", {"model": "opus"})
    write_settings(isolated["home"] / ".claude" / "settings.json", {"autoCompactWindow": 300000})
    assert compact_window(str(isolated["project"])) == 300000


def test_a_window_for_the_model_wins_within_its_file(isolated):
    write_settings(isolated["home"] / ".claude" / "settings.json", {
        "autoCompactWindow": 500000,
        "modelSettings": {"claude-opus-5-5": {"autoCompactWindow": 300000}},
    })
    project = str(isolated["project"])
    assert compact_window(project, "claude-opus-5-5[1m]") == 300000
    assert compact_window(project, "Claude-Opus-5-5") == 300000
    assert compact_window(project, "claude-sonnet-5-5") == 500000
    assert compact_window(project) == 500000


def test_a_closer_file_wins_over_a_model_window_further_out(isolated):
    write_settings(isolated["home"] / ".claude" / "settings.json",
                   {"modelSettings": {"claude-x": {"autoCompactWindow": 300000}}})
    write_settings(isolated["project"] / ".claude" / "settings.json", {"autoCompactWindow": 200000})
    assert compact_window(str(isolated["project"]), "claude-x") == 200000


@pytest.mark.parametrize("value", ["300k", True, 0, -5, None, 33000])
def test_window_rejects_what_is_not_a_usable_token_count(isolated, value):
    write_settings(isolated["project"] / ".claude" / "settings.json", {"autoCompactWindow": value})
    with pytest.raises(HookError):
        compact_window(str(isolated["project"]))


def test_window_rejects_broken_settings_json(isolated):
    (isolated["project"] / ".claude" / "settings.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(HookError, match="not valid JSON"):
        compact_window(str(isolated["project"]))


def test_an_unreadable_managed_file_is_skipped(isolated, monkeypatch):
    write_settings(isolated["managed"], {"autoCompactWindow": 350000})
    write_settings(isolated["home"] / ".claude" / "settings.json", {"autoCompactWindow": 300000})
    read_text = Path.read_text

    def refuse_managed(path, *args, **kwargs):
        if path == isolated["managed"]:
            raise PermissionError(path)
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", refuse_managed)
    assert compact_window(str(isolated["project"])) == 300000


def test_the_compaction_and_warning_points():
    assert compaction_point(300000) == 267000
    assert warn_point(267000) == 227000
    assert compaction_point(1000000) == 967000
    assert warn_point(967000) == 927000
    # On a small window the warning comes a quarter of the point early.
    assert warn_point(compaction_point(100000)) == 67000 - 16750


def test_claude_config_dir_moves_the_user_settings(isolated, monkeypatch):
    other = isolated["tmp"] / "profile"
    other.mkdir()
    write_settings(isolated["home"] / ".claude" / "settings.json", {"autoCompactWindow": 300000})
    write_settings(other / "settings.json", {"autoCompactWindow": 200000})
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(other))
    assert compact_window(None) == 200000


def write_agent(folder: Path, file: str, text: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / file).write_text(text, encoding="utf-8")


def test_a_subagent_inherits_the_window_or_its_own_lower_one(isolated, monkeypatch):
    write_settings(isolated["project"] / ".claude" / "settings.json", {"autoCompactWindow": 300000})
    agents = isolated["project"] / ".claude" / "agents"
    write_agent(agents, "reader.md", "---\nname: reader\nautoCompactWindow: 100000  # small\n---\nRead.\n")
    write_agent(agents / "big", "wide.md", "---\nname: wide\nautoCompactWindow: '500000'\n---\n")
    write_agent(agents, "plain.md", "---\nname: plain\ndescription: no window\n---\n")
    project = str(isolated["project"])
    assert agent_window(project, "reader") == 100000
    assert agent_window(project, "wide") == 500000
    assert agent_window(project, "plain") is None
    assert agent_window(project, "Explore") is None  # built in: no definition
    assert compact_window(project, None, 100000) == 100000
    assert compact_window(project, None, 500000) == 300000  # it only ever lowers it
    assert compact_window(project, None, None) == 300000
    monkeypatch.setenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "200000")
    assert compact_window(project, None, 100000) == 200000  # the env var wins, as documented


def test_a_project_agent_shadows_a_user_agent_of_the_same_name(isolated):
    write_agent(isolated["home"] / ".claude" / "agents", "r.md",
                "---\nname: reader\nautoCompactWindow: 150000\n---\n")
    assert agent_window(str(isolated["project"]), "reader") == 150000
    write_agent(isolated["project"] / ".claude" / "agents", "reader.md",
                "---\nname: reader\nautoCompactWindow: 120000\n---\n")
    assert agent_window(str(isolated["project"]), "reader") == 120000


def test_a_bad_window_in_an_agent_definition_is_refused(isolated):
    write_agent(isolated["project"] / ".claude" / "agents", "reader.md",
                "---\nname: reader\nautoCompactWindow: lots\n---\n")
    with pytest.raises(HookError, match="reader.md"):
        agent_window(str(isolated["project"]), "reader")


def test_an_unreadable_stray_file_does_not_break_every_subagent(isolated):
    agents = isolated["project"] / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "stray.md").write_bytes(b"\xff\xfe not utf-8")
    write_agent(agents, "reader.md", "---\nname: reader\nautoCompactWindow: 100000\n---\n")
    assert agent_window(str(isolated["project"]), "reader") == 100000
