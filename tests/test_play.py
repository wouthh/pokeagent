import json
import time
from pathlib import Path

import pytest

from pokeagent import objectives
from pokeagent.agents.claude_cli import ProviderError, SessionResult
from pokeagent.games.pokemon_gen1 import DEFAULT_CHECK, HALL_OF_FAME
from pokeagent.memory import Memory
from pokeagent.play import Game, Runner, new_run, play
from pokeagent.server import GameServer


class FakeGame:
    def __init__(self, map_id=0, in_game=True):
        self.map_id, self.in_game = map_id, in_game

    def state_text(self):
        return f"on map {self.map_id}"

    def observations(self):
        return {"map_id": self.map_id, "in_game": self.in_game}

    def location(self):
        return "Pallet Town"

    def save_state(self):
        return b"state"


def make_runner(tmp_path, results, objective=None, map_id=0, in_game=True):
    """A Runner whose provider replays `results`: (SessionResult, summary or None) per session."""
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    meta = {
        "objective": objective or "finish the game",
        "check": None if objective else DEFAULT_CHECK,
        "rom_sha1": "red",
        "family": "pokemon-gen1",
    }
    (run_dir / "run.json").write_text(json.dumps(meta))
    game = FakeGame(map_id, in_game)
    memory = Memory(tmp_path / "memory.db", "run")
    memory.open_run(meta)
    server = GameServer(game, memory, run_dir / "notes.md", tmp_path / "issues.md")
    briefs, sleeps, queue = [], [], iter(results)

    def provider(spec, on_event):
        briefs.append(spec.brief)
        result, summary = next(queue)
        session = server.sessions[spec.token]
        if summary:
            session.summary, session.next_focus, session.actions = summary, "the next thing", 3
        on_event("tool_call", {"name": "mcp__game__walk", "input": {"direction": "up"}})
        return result

    return Runner(run_dir, game, server, provider=provider, sleep=sleeps.append), briefs, sleeps


def test_sessions_carry_the_summary_and_checkpoint(tmp_path):
    runner, briefs, _ = make_runner(
        tmp_path, [(SessionResult("completed"), "found the lab"), (SessionResult("max_turns"), None)]
    )
    assert runner.loop("http://127.0.0.1:1/mcp", max_sessions=2) == "max_sessions"
    assert "none, this is the first session" in briefs[0] and "on map 0" in briefs[0]
    assert "found the lab" in briefs[1] and "the next thing" in briefs[1]
    assert json.loads((runner.run_dir / "state.json").read_text())["sessions"] == 2
    assert len(list((runner.run_dir / "checkpoints").glob("*.state"))) == 2
    assert 'tool walk {"direction": "up"}' in (runner.run_dir / "play.log").read_text()


def test_only_the_default_objective_has_the_trusted_check(tmp_path):
    runner, briefs, _ = make_runner(tmp_path / "a", [], map_id=HALL_OF_FAME)
    assert runner.loop("url") == "complete" and briefs == []
    runner, briefs, _ = make_runner(
        tmp_path / "b", [(SessionResult("completed"), None)], "catch a Pikachu", HALL_OF_FAME
    )
    assert runner.loop("url", max_sessions=1) == "max_sessions" and "No authoritative success check" in briefs[0]


def test_a_claim_pauses_the_run_until_the_user_settles_it(tmp_path):
    runner, briefs, _ = make_runner(
        tmp_path, [(SessionResult("completed"), "caught it"), (SessionResult("completed"), None)], "catch a Pikachu"
    )

    def claiming(spec, on_event):
        runner.server.sessions[spec.token].claim = "a Pikachu is in the party"
        return provider(spec, on_event)

    provider, runner.provider = runner.provider, claiming
    assert runner.loop("url") == "claimed"
    assert runner.memory.status() == ("claimed", "the agent reported the objective complete: a Pikachu is in the party")
    objectives.reject(runner.memory, "that is a Raichu")
    runner.provider = provider
    assert runner.loop("url", max_sessions=1) == "max_sessions"
    assert "The user rejected the completion claim" in briefs[1] and "that is a Raichu" in briefs[1]


def test_the_brief_carries_knowledge_and_a_generated_summary(tmp_path):
    runner, briefs, _ = make_runner(tmp_path, [(SessionResult("completed"), None)] * 2)
    memory = runner.memory
    memory.add_knowledge("map", "Pallet Town", "Oak stops you at the north exit")
    replay = runner.provider

    def acting(spec, on_event):
        session = runner.server.sessions[spec.token]
        session.actions = 2
        memory.event("action", {"outcome": "walk: blocked after 0 step(s)"}, session.number)
        return replay(spec, on_event)

    runner.provider = acting
    assert runner.loop("url", max_sessions=2) == "max_sessions"
    assert "#1 [map, hypothesis, rom] Pallet Town: Oak stops you" in briefs[0]
    assert "(no summary: session 1 ended with completed after 2 actions" in briefs[1]
    assert "walk: blocked after 0 step(s)" in briefs[1]
    assert [e["data"]["actions"] for e in memory.events("session_end")] == [2, 2]


def test_the_check_waits_for_a_game_in_progress(tmp_path):
    # Power-on RAM is noise: a map id that happens to equal the Hall of Fame must not complete the run.
    runner, briefs, _ = make_runner(tmp_path, [(SessionResult("completed"), None)], map_id=HALL_OF_FAME, in_game=False)
    assert runner.loop("url", max_sessions=1) == "max_sessions" and len(briefs) == 1


def test_usage_limit_waits_for_the_reset_and_keeps_the_summary(tmp_path):
    resets = time.time() + 3600
    results = [
        (SessionResult("completed"), "walked north"),
        (SessionResult("limit", resets_at=int(resets)), None),
        (SessionResult("completed"), None),
    ]
    runner, briefs, sleeps = make_runner(tmp_path, results)
    assert runner.loop("url", max_sessions=3) == "max_sessions"
    assert 3500 < sleeps[0] <= 3600
    assert "walked north" in briefs[2]


def test_repeated_errors_back_off_then_stop(tmp_path):
    runner, _, sleeps = make_runner(tmp_path, [(SessionResult("error", error="boom"), None)] * 5)
    assert runner.loop("url") == "errors"
    assert sleeps == [30, 60, 90, 120]


def test_lost_login_stops_the_run(tmp_path):
    runner, _, _ = make_runner(tmp_path, [(SessionResult("auth_error", error="401"), None)])
    with pytest.raises(ProviderError, match="login"):
        runner.loop("url")


def test_checkpoints_every_25_actions_and_keeps_the_last_ten(tmp_path):
    runner, _, _ = make_runner(tmp_path, [])
    for _ in range(50):
        runner.server.after_action()
    assert len(list((runner.run_dir / "checkpoints").glob("*.state"))) == 2
    for i in range(12):
        runner.checkpoint(str(i))
    assert len(list((runner.run_dir / "checkpoints").glob("*.state"))) == 10


def test_game_actions_on_the_rom(game):
    gen1, emu, bedroom = game
    emu.load_state(bedroom)
    live = Game(emu, gen1)
    text, png = live.full_state()
    assert "Reds House 2F" in text and "Whole map" in text and png.startswith(b"\x89PNG")
    assert live.location() == "Reds House 2F"
    text, png = live.act("walk_to", x=7, y=1)
    assert text.startswith("walk_to: map_changed") and "Reds House 1F" in text and png is None
    with pytest.raises(ValueError, match="button must be"):
        live.act("press", button="turbo", times=1)


def test_resume_restores_the_notes_and_progress_saved_with_the_checkpoint(tmp_path):
    runner, _, _ = make_runner(tmp_path, [])
    notes, state = runner.run_dir / "notes.md", runner.run_dir / "state.json"
    notes.write_text("old notes")
    state.write_text('{"sessions": 1}')
    runner.checkpoint("a")
    notes.write_text("notes from a lost timeline")
    state.write_text('{"sessions": 2}')
    runner.restore_context(sorted((runner.run_dir / "checkpoints").glob("*.state"))[-1])
    assert notes.read_text() == "old notes" and state.read_text() == '{"sessions": 1}'
    for i in range(12):
        runner.checkpoint(str(i))
    folder = runner.run_dir / "checkpoints"
    assert len(list(folder.glob("*.notes.md"))) == len(list(folder.glob("*.state"))) == 10


def test_a_relative_run_dir_becomes_absolute(tmp_path, monkeypatch):
    runner, _, _ = make_runner(tmp_path, [])
    monkeypatch.chdir(tmp_path)
    rel = Runner(Path("run"), runner.game, runner.server)
    assert rel.run_dir.is_absolute()


def test_new_runs_record_their_checks_and_settled_runs_do_not_resume(rom_path, tmp_path):
    default = json.loads((new_run(rom_path, None, tmp_path / "a") / "run.json").read_text())
    assert (default["check"], default["success"]) == (DEFAULT_CHECK, None)
    badge = {"obs": "badges", "op": ">=", "value": 1}
    run_dir = new_run(rom_path, "earn a badge", tmp_path / "b", badge)
    meta = json.loads((run_dir / "run.json").read_text())
    assert (meta["check"], meta["success"]) == (None, badge)
    with pytest.raises(ValueError, match="unknown observation"):
        new_run(rom_path, "earn a badge", tmp_path / "c", {"obs": "gyms", "op": ">=", "value": 1})
    memory = Memory(tmp_path / "b" / "memory.db", run_dir.name)
    memory.open_run(meta)
    assert [(c["source"], c["condition"]) for c in memory.checks("active")] == [("user", badge)]
    objectives.claim(memory, "the agent reported the objective complete: badge")
    with pytest.raises(ValueError, match="settle it with `pokeagent objective confirm"):
        play(run_dir, tmp_path / "b")
