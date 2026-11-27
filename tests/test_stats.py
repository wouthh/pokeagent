import json

from pokeagent import cli, stats
from pokeagent.memory import Memory

META = {"objective": "finish the game", "rom_sha1": "red", "family": "pokemon-gen1"}


def played(tmp_path, run, cost, waited=0.0):
    """A run with one play session that reached two milestones and a develop session."""
    memory = Memory(tmp_path / "memory.db", run)
    memory.open_run(META)
    usage = {"input_tokens": 1000, "output_tokens": 500, "cache_read_input_tokens": 9000}
    memory.event("session_start", {}, 1)
    memory.event("milestone", {"kind": "map", "map_id": 1, "text": "first visit to Pallet Town"}, 1)
    memory.event("action", {"outcome": "walk"}, 1)
    if waited:
        memory.event("limit_wait", {"seconds": waited})
    memory.event("capability_call", {"id": 1, "name": "heal", "version": 1, "error": "stuck"}, 1)
    memory.event("milestone", {"kind": "badge", "badges": 1, "text": "badge 1"}, 1)
    memory.event("action", {"outcome": "walk"}, 1)
    memory.event("session_end", {"end": "completed", "turns": 9, "actions": 2, "cost_usd": cost, "usage": usage}, 1)
    memory.event("session_start", {"mode": "develop"}, 2)
    memory.event("development_cycle", {"tool": "step", "staged": 1, "attempt": 1}, 2)
    end = {"end": "completed", "turns": 5, "actions": 0, "cost_usd": 0.5, "usage": {"output_tokens": 500}}
    memory.event("session_end", end | {"mode": "develop"}, 2)
    return memory


def test_report_has_a_row_per_session_and_the_development_share(tmp_path):
    text = stats.report(played(tmp_path, "a", 1.25))
    rows = text.splitlines()
    assert rows[1].split()[:8] == ["1", "play", "completed", "9", "2", "1", "1", "2"]
    assert "1,500" in rows[1] and "9,000" in rows[1] and "$  1.25" in rows[1]
    assert rows[2].split()[:3] == ["2", "develop", "completed"]
    assert "play: 1 sessions, 2 actions, 1,500 tokens, $1.25" in text
    assert "development share: 25% of tokens (cache reads excluded)" in text


def test_compare_lines_up_shared_milestones(tmp_path):
    a, b = played(tmp_path, "a", 1.0), played(tmp_path, "b", 2.0, waited=3600)
    b.event("milestone", {"kind": "badge", "badges": 2, "text": "badge 2"}, 3)
    lines = stats.compare(a, b).splitlines()
    assert [line.split("|")[0].strip() for line in lines[1:]] == ["first visit to Pallet Town", "badge 1"]
    badge = lines[2].split("|")
    assert badge[1].split() == ["1", "2", "1,500", "$", "1.00", "0.0"]  # two actions before it
    assert badge[2].split()[:5] == ["1", "2", "1,500", "$", "2.00"]
    assert stats.compare(a, Memory(tmp_path / "memory.db", "c")).endswith("no milestone in common yet")


def test_status_and_the_cli(tmp_path, capsys):
    data = tmp_path / "data"
    run_dir = data / "runs" / "a"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps(META))
    (run_dir / "state.json").write_text(json.dumps({"sessions": 2, "next_focus": "beat Brock"}))
    memory = played(data, "a", 1.0)
    memory.add_knowledge("map", "Pewter City", "the gym is north")
    text = stats.status(memory, run_dir, "session 3 (play)")
    assert text.startswith("Run a: running; now session 3 (play)\nObjective: finish the game")
    assert "Sessions: 1 play, 1 develop; 2 actions, 2,000 tokens, $1.50; timeline 1" in text
    assert "Planned focus: beat Brock" in text and "Milestones: first visit to Pallet Town; badge 1" in text
    assert "Recent knowledge: Pewter City (hypothesis)" in text
    assert "Development: 1 cycle(s); last for step: staged version 1" in text
    memory.close()
    assert cli.main(["status", str(run_dir)]) == 0 and "Planned focus: beat Brock" in capsys.readouterr().out
    played(data, "b", 2.0).close()
    (data / "runs" / "b").mkdir()
    (data / "runs" / "b" / "run.json").write_text(json.dumps(META))
    assert cli.main(["stats", "a", "--data-dir", str(data), "--compare", "b"]) == 0
    out = capsys.readouterr().out
    assert "development share" in out and "badge 1" in out


def test_elapsed_time_leaves_out_limit_waits(tmp_path, monkeypatch):
    now = [0.0]

    def tick():  # every write is ten minutes after the one before
        now[0] += 600
        return now[0]

    monkeypatch.setattr("pokeagent.memory.time.time", tick)
    memory = played(tmp_path, "a", 1.0, waited=1200)
    start = memory.events("session_start", limit=-1)[0]["created"]
    badge = memory.events("milestone", limit=-1)[1]["created"]
    assert stats._reached(memory)["badge 1"]["minutes"] == (badge - start - 1200) / 60 > 0


def test_an_interrupted_session_is_reported(tmp_path):
    memory = Memory(tmp_path / "memory.db", "r")
    memory.open_run(META)
    memory.event("session_start", {}, 1)
    memory.event("action", {"outcome": "walk"}, 1)
    memory.event("milestone", {"kind": "map", "map_id": 1, "text": "first visit to Pallet Town"}, 1)
    (row,) = stats.sessions(memory)
    assert (row["end"], row["actions"], row["milestones"], row["mode"]) == ("interrupted", 1, 1, "play")
    assert "interrupted" in stats.report(memory)
