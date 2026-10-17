"""Where a project's sessions spend their tokens — to compare before and after.

Costs are relative units (input 1, cache read 0.1, 1-hour cache write 2,
5-minute cache write 1.25, output 5), so the comparison holds whatever the
model's price. Sessions are selected by when they started: a session keeps the
compaction window it started with, so one started before the window was set
belongs to "before" even if it ran on afterwards. Subagents are counted apart,
under the session that started them.
"""

from __future__ import annotations

import collections
import datetime as dt
import re
import statistics
from pathlib import Path
from typing import Optional

from .settings import config_dir
from .transcript import records, usage_tokens

BANDS = [(100_000, "<100k"), (200_000, "100-200k"), (300_000, "200-300k"),
         (400_000, "300-400k"), (700_000, "400-700k"), (float("inf"), ">700k")]
WEIGHTS = {"read": 0.1, "output": 5, "input": 1}
# Sessions that never grew past this are one-shot probes (`claude -p`, hook
# tests) and would only blur the peak statistics.
PROBE_PEAK = 60_000


def transcripts_dir(project: Path) -> Path:
    slug = re.sub(r"[^A-Za-z0-9-]", "-", str(project.resolve()))
    return config_dir() / "projects" / slug


def analyse(path: Path, sidechain: bool = False) -> Optional[dict]:
    seen = set()
    cost = collections.Counter()
    by_band = collections.Counter()
    peak = turns = context_sum = compactions = 0
    started = None
    for _, record in records(path):
        if started is None and record.get("timestamp"):
            started = record["timestamp"]
        if record.get("type") == "system" and record.get("subtype") == "compact_boundary":
            compactions += 1
        if record.get("type") != "assistant" or bool(record.get("isSidechain")) != sidechain:
            continue
        message = record.get("message") or {}
        if message.get("id") in seen:
            continue  # streamed chunks repeat one message's usage
        seen.add(message.get("id"))
        usage = message.get("usage") or {}
        context = usage_tokens(usage)
        if not context:
            continue
        writes = usage.get("cache_creation") or {}
        parts = {
            "read": WEIGHTS["read"] * usage.get("cache_read_input_tokens", 0),
            "write": 2 * writes.get("ephemeral_1h_input_tokens", 0)
            + 1.25 * writes.get("ephemeral_5m_input_tokens", 0),
            "output": WEIGHTS["output"] * usage.get("output_tokens", 0),
            "input": WEIGHTS["input"] * usage.get("input_tokens", 0),
        }
        cost.update(parts)
        by_band[_band(context)] += sum(parts.values())
        peak = max(peak, context)
        turns += 1
        context_sum += context
    if not turns or started is None:
        return None
    return {"path": path, "cost": cost, "by_band": by_band, "peak": peak, "turns": turns,
            "context_sum": context_sum, "compactions": compactions,
            "started": dt.datetime.fromisoformat(started.replace("Z", "+00:00"))}


def report(project: Path, since: Optional[dt.date], until: Optional[dt.date]) -> str:
    folder = transcripts_dir(project)
    sessions = [s for s in map(analyse, sorted(folder.glob("*.jsonl"))) if s]
    if since:
        sessions = [s for s in sessions if s["started"].date() >= since]
    if until:
        sessions = [s for s in sessions if s["started"].date() < until]
    if not sessions:
        return f"no sessions in {folder} for that range"
    total = sum((s["cost"] for s in sessions), collections.Counter())
    bands = sum((s["by_band"] for s in sessions), collections.Counter())
    spend = sum(total.values())
    turns = sum(s["turns"] for s in sessions)
    peaks = [s["peak"] for s in sessions if s["peak"] > PROBE_PEAK] or [s["peak"] for s in sessions]
    lines = [
        f"{len(sessions)} sessions, {turns} turns, in {folder}",
        f"cost per turn: {spend / turns / 1000:.1f}k units; "
        f"average context {sum(s['context_sum'] for s in sessions) / turns / 1000:.0f}k",
        "spend:   " + "  ".join(f"{k} {100 * total[k] / spend:.0f}%" for k in ("read", "write", "output", "input")),
        "by context at the turn: " + "  ".join(
            f"{label} {100 * bands[label] / spend:.0f}%" for _, label in BANDS if bands[label]),
        f"peak context per session: median {statistics.median(peaks) // 1000:.0f}k, max {max(peaks) // 1000}k",
        f"sessions above 400k: {sum(p > 400_000 for p in peaks)}; "
        f"auto-compactions: {sum(s['compactions'] for s in sessions)}",
    ]
    agents = [a for s in sessions
              for a in (analyse(p, sidechain=True)
                        for p in sorted((s["path"].with_suffix("") / "subagents").glob("*.jsonl"))) if a]
    if agents:
        agent_spend = sum(sum(a["cost"].values()) for a in agents)
        agent_turns = sum(a["turns"] for a in agents)
        lines.append(
            f"subagents (not counted above): {len(agents)}, {agent_turns} turns, "
            f"cost per turn {agent_spend / agent_turns / 1000:.1f}k units, average context "
            f"{sum(a['context_sum'] for a in agents) / agent_turns / 1000:.0f}k, "
            f"peak max {max(a['peak'] for a in agents) // 1000}k, "
            f"auto-compactions {sum(a['compactions'] for a in agents)}; "
            f"{100 * agent_spend / (spend + agent_spend):.0f}% of all spend")
    return "\n".join(lines)


def _band(tokens: int) -> str:
    return next(label for limit, label in BANDS if tokens < limit)
