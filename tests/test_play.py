import json
import time

import pytest

from pokeagent.agents.claude_cli import ProviderError, SessionResult
from pokeagent.games.pokemon_gen1 import DEFAULT_CHECK, HALL_OF_FAME
from pokeagent.play import Game, Runner
from pokeagent.server import GameServer


class FakeGame:
    def __init__(self, map_id=0, in_game=True):
        self.map_id, self.in_game = map_id, in_game

    def state_text(self):
        return f"on map {self.map_id}"

    def observations(self):
        return {"map_id": self.map_id, "in_game": self.in_game}

    def save_state(self):
        return b"state"


def make_runner(tmp_path, results, objective=None, map_id=0, in_game=True):
    """A Runner whose provider replays `results`: (SessionResult, summary or None) per session."""
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    meta = {"objective": objective or "finish the game", "check": None if objective else DEFAULT_CHECK}
    (run_dir / "run.json").write_text(json.dumps(meta))
    game = FakeGame(map_id, in_game)
    server = GameServer(game, run_dir / "notes.md", tmp_path / "issues.md", "run")
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
    assert runner.loop("url", max_sessions=1) == "max_sessions" and "Success check: none" in briefs[0]


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
    text, png = live.act("walk_to", x=7, y=1)
    assert text.startswith("walk_to: map_changed") and "Reds House 1F" in text and png is None
    with pytest.raises(ValueError, match="button must be"):
        live.act("press", button="turbo", times=1)
