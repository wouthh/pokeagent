import json
import threading
import time
from pathlib import Path

import pytest

from pokeagent import objectives
from pokeagent import play as play_module
from pokeagent.agents.claude_cli import ProviderError, SessionResult
from pokeagent.capabilities import Registry, check_macro, check_observation
from pokeagent.emulator.client import Emulator, EmulatorError
from pokeagent.emulator.cores import core_path
from pokeagent.games.pokemon_gen1 import CORE, CORE_OPTIONS, DEFAULT_CHECK, HALL_OF_FAME, OBSERVATIONS
from pokeagent.memory import Memory
from pokeagent.play import Game, Runner, latest_state, new_run, play, restorable, restore, run_lock
from pokeagent.server import GameServer


class FakeGame:
    def state_text(self):
        return f"on map {self.map_id}"

    def observations(self):
        return {"map_id": self.map_id, "in_game": self.in_game}

    def location(self):
        return "Pallet Town"

    def __init__(self, map_id=0, in_game=True):
        self.map_id, self.in_game, self.saves, self.restarts = map_id, in_game, 0, []

    def save_state(self):
        self.saves += 1
        return f"state {self.saves}".encode()

    def restart(self, state):
        self.restarts.append(state)


def make_runner(tmp_path, results, objective=None, map_id=0, in_game=True):
    """A Runner whose provider replays `results`: (SessionResult, summary or None) per session."""
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    meta = {
        "objective": objective or "finish the game",
        "check": None if objective else DEFAULT_CHECK,
        "rom_sha1": "red",
        "family": "pokemon-gen1",
        "core": "sameboy",
        "core_options": {"sameboy_model": "Game Boy"},
    }
    (run_dir / "run.json").write_text(json.dumps(meta))
    game = FakeGame(map_id, in_game)
    memory = Memory(tmp_path / "memory.db", "run")
    memory.open_run(meta)
    game.registry = Registry(memory)
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
        if session.number == 1:
            spec_ = check_macro(
                {"name": "spin", "description": "turn around", "steps": [{"do": "walk"}]}, set(), set(), set()
            )
            registry = runner.game.registry
            registry.define("macro", spec_, "rom", 1)
            registry.rollback(registry.define("macro", spec_, "rom", 1), "it kept failing", 1)
        return replay(spec, on_event)

    runner.provider = acting
    assert runner.loop("url", max_sessions=2) == "max_sessions"
    assert "#1 [map, hypothesis, rom] Pallet Town: Oak stops you" in briefs[0]
    assert "(no summary: session 1 ended with completed after 2 actions" in briefs[1]
    assert "walk: blocked after 0 step(s)" in briefs[1]
    assert "spin version 2 was rolled back (it kept failing); version 1 is active again" in briefs[1]
    assert "spin (macro v1, rom; no params; unused): turn around" in briefs[1]
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


def test_a_checkpoint_action_saves_the_emulator_once(tmp_path):
    runner, _, _ = make_runner(tmp_path, [])
    for _ in range(25):
        runner.server.after_action()
    assert runner.game.saves == 25  # the 25th action's checkpoint reuses its recovery state
    assert sorted((runner.run_dir / "checkpoints").glob("*.state"))[-1].read_bytes() == latest_state(runner.run_dir)


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


def test_observations_and_macros_on_the_rom(game, tmp_path):
    gen1, emu, bedroom = game
    emu.load_state(bedroom)
    memory = Memory(tmp_path / "memory.db", "run")
    memory.open_run({"objective": "x", "rom_sha1": "red", "family": "pokemon-gen1"})
    live = Game(emu, gen1, registry=Registry(memory))
    x = live.observations()["x"]
    spec = check_observation(
        {"name": "my_x", "description": "the player's x", "address": 0xD362, "decode": "u8"}, set(OBSERVATIONS)
    )
    assert live.try_observation(spec) == x
    live.registry.define("observation", spec, "rom", 1)
    assert f"Your observations: my_x={x}" in live.full_state()[0] and f"my_x={x}" in live.state_text()

    def define(name, steps):
        live.registry.define(
            "macro",
            check_macro({"name": name, "description": name, "steps": steps}, {"my_x", "map_id"}, set(), set()),
            "rom",
            1,
        )

    define(
        "two_right",
        [
            {
                "do": "walk",
                "args": {"direction": "right"},
                "until": {"obs": "my_x", "op": "==", "value": x + 2},
                "max": 4,
            }
        ],
    )
    text, _, _, error = live.run_macro("two_right", {})
    assert (
        error is None
        and text.startswith("run_macro two_right: done after 2 action(s)")
        and live.observations()["x"] == x + 2
    )
    define(
        "nowhere",
        [{"do": "press", "args": {"button": "b"}, "until": {"obs": "map_id", "op": "==", "value": 200}, "max": 2}],
    )
    define("badtype", [{"do": "walk", "args": {"direction": []}}])
    _, _, _, error = live.run_macro("badtype", {})
    assert error and "direction must be one of" in error
    text, _, macro, error = live.run_macro("nowhere", {})
    assert "until did not hold after 2 times" in error and text.startswith("run_macro nowhere: failed:")
    assert "Now: Reds House 2F" in text and macro["name"] == "nowhere"


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


def test_every_action_saves_the_recovery_state_and_a_restart_loads_it(tmp_path):
    runner, _, _ = make_runner(tmp_path, [])
    assert latest_state(runner.run_dir) is None  # power-on
    runner.checkpoint("start")
    assert latest_state(runner.run_dir) == b"state 1"
    runner.server.after_action()
    assert latest_state(runner.run_dir) == b"state 2"
    assert [runner.recover("worker exited") for _ in range(4)] == [True, True, True, False]
    assert runner.game.restarts == [b"state 2"] * 3 and len(runner.memory.events("restart")) == 3


def test_a_failed_recovery_write_is_an_emulator_failure_so_the_server_restarts(tmp_path, monkeypatch):
    runner, _, _ = make_runner(tmp_path, [])
    runner.server.after_action()

    def full_disk(path, data):
        raise OSError("no space left on device")

    monkeypatch.setattr("pokeagent.play.write_atomic", full_disk)
    with pytest.raises(EmulatorError, match="no space left"):
        runner.server.after_action()
    assert runner.recover("write failed") and runner.game.restarts == [b"state 1"]


def test_overlapping_actions_never_leave_an_older_recovery_state(tmp_path):
    runner, _, _ = make_runner(tmp_path, [])
    game, first_saved, release = runner.game, threading.Event(), threading.Event()
    original = game.save_state

    def slow_first_save():
        state = original()
        if state == b"state 1":
            first_saved.set()
            release.wait(0.5)  # the second action tries to overtake here
        return state

    game.save_state = slow_first_save
    first = threading.Thread(target=runner.server.after_action)
    first.start()
    assert first_saved.wait(5)
    second = threading.Thread(target=runner.server.after_action)
    second.start()
    release.set()
    first.join()
    second.join()
    assert latest_state(runner.run_dir) == b"state 2"


def test_a_restart_waits_for_an_overlapping_actions_recovery_write(tmp_path):
    runner, _, _ = make_runner(tmp_path, [])
    game, first_saved, release = runner.game, threading.Event(), threading.Event()
    original = game.save_state

    def slow_save():
        state = original()
        first_saved.set()
        release.wait(0.5)  # the restart tries to overtake here
        return state

    game.save_state = slow_save
    action = threading.Thread(target=runner.server.after_action)
    action.start()
    assert first_saved.wait(5)
    restart = threading.Thread(target=runner.recover, args=("worker exited",))
    restart.start()
    release.set()
    action.join()
    restart.join()
    assert game.restarts == [b"state 1"]


def test_restore_branches_a_timeline_and_the_brief_says_what_changed(tmp_path):
    runner, briefs, _ = make_runner(tmp_path, [(SessionResult("completed"), None)])
    memory, run_dir = runner.memory, runner.run_dir
    notes, progress = run_dir / "notes.md", run_dir / "state.json"
    notes.write_text("go to Viridian\nthe Mart has the parcel\n")
    progress.write_text(json.dumps({"sessions": 1, "summary": "left Pallet", "next_focus": "the Mart"}))
    runner.checkpoint("session 1")
    checkpoint = restorable(run_dir, memory)[-1]["data"]["file"]
    first = memory.timeline()
    for _ in range(3):
        runner.server.after_action()
        memory.event("action", {"outcome": "walk: moved"}, 2)
    delivered = memory.add_knowledge("progress", "Parcel", "delivered", scope="run")
    notes.write_text("go to Viridian\nOak gave me the Pokédex\n")
    progress.write_text(json.dumps({"sessions": 4, "summary": "delivered the parcel", "next_focus": "Route 2"}))
    runner.checkpoint("session 4")

    memory.set_status("complete", "done")
    done = restore(run_dir, memory, checkpoint)
    assert memory.status()[0] == "running"
    assert done["undone_actions"] == 3 and done["stale"] == [delivered]
    assert latest_state(run_dir) == b"state 1" and notes.read_text() == "go to Viridian\nthe Mart has the parcel\n"
    assert json.loads(progress.read_text()) == {"sessions": 4, "summary": "left Pallet", "next_focus": "the Mart"}
    assert memory.timeline() != first and [e["data"]["reason"] for e in restorable(run_dir, memory)] == ["session 1"]
    with pytest.raises(ValueError, match="not a saved checkpoint"):  # the undone part is not in this history
        restore(run_dir, memory, sorted((run_dir / "checkpoints").glob("*.state"))[-1].name)
    assert runner.loop("url", max_sessions=1) == "max_sessions"
    assert "restored to the checkpoint saved" in briefs[0] and "3 later actions were undone" in briefs[0]
    assert f"now stale: #{delivered}" in briefs[0] and "  Oak gave me the Pokédex" in briefs[0]
    assert "Last session's summary: left Pallet" in briefs[0] and "This is session 5" in briefs[0]


def test_restore_that_fails_writing_files_leaves_the_run_unchanged(tmp_path, monkeypatch):
    runner, _, _ = make_runner(tmp_path, [(SessionResult("completed"), None)])
    memory, run_dir = runner.memory, runner.run_dir
    (run_dir / "notes.md").write_text("old notes\n")
    runner.checkpoint("session 1")
    checkpoint = restorable(run_dir, memory)[-1]["data"]["file"]
    runner.server.after_action()
    (run_dir / "notes.md").write_text("later notes\n")
    before, timeline = latest_state(run_dir), memory.timeline()
    real = play_module.write_atomic

    def failing(path, data):
        if path.name == "notes.md" and data == b"old notes\n":
            raise OSError("disk full")
        real(path, data)

    monkeypatch.setattr(play_module, "write_atomic", failing)
    with pytest.raises(OSError):
        restore(run_dir, memory, checkpoint)
    monkeypatch.setattr(play_module, "write_atomic", real)
    assert memory.timeline() == timeline and latest_state(run_dir) == before
    assert (run_dir / "notes.md").read_text() == "later notes\n"


def test_restore_refuses_a_checkpoint_of_another_core_setup(tmp_path):
    runner, _, _ = make_runner(tmp_path, [])
    runner.meta["core_options"] = {"sameboy_model": "Game Boy Color"}
    runner.checkpoint("other model")
    with pytest.raises(ValueError, match="another ROM, core or core options"):
        restore(runner.run_dir, runner.memory, restorable(runner.run_dir, runner.memory)[-1]["data"]["file"])


def test_one_process_per_run(tmp_path):
    with run_lock(tmp_path), pytest.raises(ValueError, match="in use"), run_lock(tmp_path):
        pass


def test_the_game_restarts_at_a_saved_state(game, rom_path):
    gen1, _, bedroom = game
    started = []

    def start():
        started.append(Emulator(core_path(CORE), rom_path, options=CORE_OPTIONS))
        return started[-1]

    live = Game(start(), gen1, start=start)
    live.load_state(bedroom)
    before = live.state_text()
    live.emu.close()  # as if the worker died
    live.restart(bedroom)
    assert live.state_text() == before and live.emu is started[-1]
    live.emu.close()
