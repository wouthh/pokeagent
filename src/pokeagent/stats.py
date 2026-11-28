"""Run metrics from the memory store, for `pokeagent status`, `pokeagent stats [--compare]` and the viewer panel.

Everything comes from events: `session_start` and `session_end` (mode, end, turns, actions, usage, cost),
`action`, `milestone`, `capability_call`, `stuck`, `limit_wait` and `development_cycle`. The CLI reports usage only
at a session's end, so a milestone is charged its whole session's tokens and cost. Elapsed time leaves out usage
limit waits. The numbers are reported as measured.
"""

import fcntl
import json
from collections import Counter
from pathlib import Path

from pokeagent import capabilities, develop, objectives
from pokeagent.memory import Memory


def live(run_dir: Path) -> bool:
    """Whether a pokeagent process plays or restores the run now (it holds the run's lock)."""
    try:
        with (run_dir / ".lock").open("rb") as f:
            fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except FileNotFoundError:
        return False
    except BlockingIOError:
        return True
    return False


def sessions(memory: Memory, live: bool = False) -> list[dict]:
    """One row per session, oldest first. A session without `session_end` is "running" if it is the last one of a
    live run, else "interrupted" (stopped by Ctrl-C), with its actions counted from events and no turns, usage or
    cost (reported at the end)."""
    ends = {e["session"]: e for e in memory.events("session_end", limit=-1)}
    calls = memory.events("capability_call", limit=-1)
    milestones = memory.events("milestone", limit=-1)
    starts = memory.events("session_start", limit=-1)
    rows = []
    for start in starts:
        n = start["session"]
        mine = [c for c in calls if c["session"] == n]
        acted = memory.events("action", session=n, limit=-1)
        end = ends.get(n)
        open_end = "running" if live and start is starts[-1] else "interrupted"
        data = end["data"] if end else {"end": open_end, "turns": 0, "actions": len(acted), "cost_usd": 0.0}
        data = {"mode": start["data"].get("mode", "play")} | data
        usage = data.get("usage") or {}
        stopped = end["created"] if end else max([start["created"]] + [e["created"] for e in acted + mine])
        rows.append(
            {
                "session": n,
                "mode": data.get("mode", "play"),
                "end": data["end"],
                "turns": data["turns"],
                "actions": data["actions"],
                "calls": len(mine),
                "failed": sum(1 for c in mine if c["data"]["error"]),
                "milestones": sum(1 for m in milestones if m["session"] == n),
                "tokens": develop.tokens(usage),
                "cache_read": usage.get("cache_read_input_tokens") or 0,
                "cost": data["cost_usd"],
                "minutes": (stopped - start["created"]) / 60,
            }
        )
    return rows


def report(memory: Memory, live: bool = False) -> str:
    rows = sessions(memory, live)
    head = f"{'session':>7} {'mode':7} {'end':10} {'turns':>5} {'actions':>7} {'calls':>5} {'failed':>6}"
    head += f" {'milestones':>10} {'tokens':>10} {'cache read':>11} {'cost':>7} {'minutes':>7}"
    lines = [head]
    for r in rows:
        lines.append(
            f"{r['session']:>7} {r['mode']:7} {r['end']:10} {r['turns']:>5} {r['actions']:>7} {r['calls']:>5}"
            f" {r['failed']:>6} {r['milestones']:>10} {r['tokens']:>10,} {r['cache_read']:>11,} ${r['cost']:>6.2f}"
            f" {r['minutes']:>7.1f}"
        )
    for mode in ("play", "develop"):
        mine = [r for r in rows if r["mode"] == mode]
        if mine:
            lines.append(
                f"{mode}: {len(mine)} sessions, {sum(r['actions'] for r in mine)} actions,"
                f" {sum(r['tokens'] for r in mine):,} tokens, ${sum(r['cost'] for r in mine):.2f},"
                f" {sum(r['minutes'] for r in mine):.0f} min"
            )
    total = sum(r["tokens"] for r in rows)
    if total:
        dev = sum(r["tokens"] for r in rows if r["mode"] == "develop")
        lines.append(f"development share: {dev / total:.0%} of tokens (cache reads excluded)")
    return "\n".join(lines)


RECENT = 5  # play sessions the brief's efficiency line covers


def _call(data: dict) -> str:
    """An action as `name key=value ...`, such as `press button=a times=1`."""
    args = (f"{k}={v if isinstance(v, str | int) else json.dumps(v)}" for k, v in data["args"].items() if v != {})
    return " ".join([data["name"], *args])


def efficiency(memory: Memory) -> list[str]:
    """Brief lines on where recent play went: actions, milestones and cost of the last play sessions, and the last
    one's most repeated actions and three-action sequences. Numbers only; what they mean is the agent's call."""
    rows = [r for r in sessions(memory) if r["mode"] == "play"][-RECENT:]
    if not rows:
        return []
    lines = [
        "Recent play sessions (actions, milestones, cost): "
        + "; ".join(f"session {r['session']}: {r['actions']}, {r['milestones']}, ${r['cost']:.2f}" for r in rows)
    ]
    last = rows[-1]["session"]
    calls = [_call(e["data"]) for e in memory.events("action", session=last, limit=-1)]
    if repeated := [f"{call} x{n}" for call, n in Counter(calls).most_common(5) if n > 1]:
        lines.append(f"Most repeated actions in session {last}: " + "; ".join(repeated))
    triples = Counter(tuple(calls[i : i + 3]) for i in range(len(calls) - 2) if len(set(calls[i : i + 3])) > 1)
    if sequences := [f"{' > '.join(t)} x{n}" for t, n in triples.most_common(3) if n > 2]:
        lines.append(f"Most repeated three-action sequences in session {last}: " + "; ".join(sequences))
    return lines


def _reached(memory: Memory) -> dict[str, dict]:
    """Each milestone's first occurrence, by text, with the run's totals up to it."""
    starts = memory.events("session_start", limit=-1)
    begun = starts[0]["created"] if starts else 0.0
    ends = memory.events("session_end", limit=-1)
    waits = memory.events("limit_wait", limit=-1)
    actions = [e["id"] for e in memory.events("action", limit=-1)]
    found = {}
    for m in memory.events("milestone", limit=-1):
        if m["data"]["text"] in found:
            continue
        charged = [e for e in ends if e["session"] is not None and e["session"] <= (m["session"] or 0)]
        waited = sum(w["data"]["seconds"] for w in waits if w["created"] < m["created"])
        found[m["data"]["text"]] = {
            "session": m["session"],
            # the runner records a milestone just before the action that reached it: count that action too
            "actions": sum(1 for a in actions if a < m["id"]) + 1,
            "tokens": sum(develop.tokens(e["data"].get("usage")) for e in charged),
            "cost": sum(e["data"]["cost_usd"] for e in charged),
            "minutes": max(0.0, m["created"] - begun - waited) / 60,
        }
    return found


def compare(a: Memory, b: Memory) -> str:
    """The milestones both runs reached, with what each spent to get there."""
    ra, rb = _reached(a), _reached(b)
    shared = [text for text in ra if text in rb]
    if not shared:
        return f"{a.run} and {b.run} have no milestone in common yet"
    width = max(len(text) for text in shared)

    def cell(r: dict) -> str:
        return f"{r['session']:>7} {r['actions']:>7} {r['tokens']:>11,} ${r['cost']:>6.2f} {r['minutes']:>7.1f}"

    columns = f"{'session':>7} {'actions':>7} {'tokens':>11} {'cost':>7} {'minutes':>7}"
    lines = [f"{'milestone':{width}} | {a.run}: {columns} | {b.run}: {columns}"]
    lines += [
        f"{text:{width}} | {' ' * (len(a.run) + 2)}{cell(ra[text])} | {' ' * (len(b.run) + 2)}{cell(rb[text])}"
        for text in shared
    ]
    return "\n".join(lines)


def status(memory: Memory, run_dir: Path, now: str = "") -> str:
    """A few lines on where a run stands: `pokeagent status` and the viewer panel."""
    state, reason = memory.status()
    if state == "running" and not live(run_dir):  # the store says running until the run ends or pauses
        state = "stopped, resume continues it"
    meta = json.loads((run_dir / "run.json").read_text())
    progress_file = run_dir / "state.json"
    progress = json.loads(progress_file.read_text()) if progress_file.exists() else {}
    rows = sessions(memory)
    lines = [f"Run {memory.run}: {state}" + (f" ({reason})" if reason else "") + (f"; now {now}" if now else "")]
    lines.append(f"Objective: {meta['objective']}")
    style = meta["style"] + (", glitches allowed" if meta["glitches"] else "")
    lines.append(f"Style: {style}" + (f"; started from run {meta['from']}" if meta["from"] else ""))
    lines += [f"Check ({c['source']}): {objectives.describe(c['condition'])}" for c in memory.checks("active")]
    play = [r for r in rows if r["mode"] == "play"]
    lines.append(
        f"Sessions: {len(play)} play, {len(rows) - len(play)} develop; {sum(r['actions'] for r in rows)} actions,"
        f" {sum(r['tokens'] for r in rows):,} tokens, ${sum(r['cost'] for r in rows):.2f}; timeline"
        f" {memory.timeline()}"
    )
    lines.append(f"Planned focus: {progress.get('next_focus') or 'none'}")
    history = memory.history()
    reached = [e for e in memory.events("milestone", limit=-1) if e["created"] <= history.get(e["timeline"], -1)]
    lines.append("Milestones: " + ("; ".join(e["data"]["text"] for e in reached[-5:]) or "none yet"))
    if found := memory.events("stuck", limit=1):
        lines.append(f"Last stuck notice: level {found[0]['data']['level']} in session {found[0]['session']}")
    registry = capabilities.Registry(memory)
    if active := sorted(registry.active(), key=lambda c: c["id"], reverse=True):
        names = ", ".join(f"{c['name']} ({c['kind']} v{c['version']})" for c in active[:8])
        lines.append(f"Capabilities ({len(active)}, newest first): {names}")
    recent = memory.rows("SELECT topic, status FROM knowledge WHERE run = ? ORDER BY id DESC LIMIT 3", memory.run)
    if recent:
        lines.append("Recent knowledge: " + "; ".join(f"{r['topic']} ({r['status']})" for r in recent))
    if cycles := memory.events("development_cycle", limit=-1):
        last = cycles[-1]["data"]
        outcome = f"staged version {last['staged']}" if last["staged"] else "nothing validated"
        lines.append(f"Development: {len(cycles)} cycle(s); last for {last['tool']}: {outcome}")
    if ideas := memory.events("objective_suggested", limit=3):
        lines.append("Objectives the agent suggested for later runs:")
        lines += [f"  {e['data']['objective']} ({e['data']['why']})" for e in ideas]
    return "\n".join(lines)
