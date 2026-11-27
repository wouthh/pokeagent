import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from pokeagent import objectives, stuck
from pokeagent import play as play_module
from pokeagent.agents.claude_cli import ProviderError, SessionResult
from pokeagent.capabilities import Registry, check_macro, check_observation
from pokeagent.develop import Bench
from pokeagent.emulator.client import Emulator, EmulatorError
from pokeagent.emulator.cores import core_path
from pokeagent.extensions import install
from pokeagent.games.pokemon_gen1 import CORE, CORE_OPTIONS, DEFAULT_CHECK, HALL_OF_FAME, OBSERVATIONS, Gen1
from pokeagent.memory import Memory
from pokeagent.play import Game, Runner, latest_state, new_run, play, restorable, restore, run_lock
from pokeagent.server import GameServer
from pokeagent.validation import validate

WALK = {"name": "walk", "args": {"direction": "up", "steps": 1}}


class FakeGame:
    adapter, viewer = Gen1, None  # Gen1 for its progress score

    def state_text(self):
        return f"on map {self.map_id}"

    def progress_state(self):
        place = {"in_game": self.in_game, "map_id": self.map_id, "map": "Pallet Town", "x": self.x, "y": 0}
        return place | {"badges": self.badges, "dex_owned": 0, "party": [{"level": 5, "exp": self.exp}]}

    def load_state(self, state):
        self.loaded = state

    def observations(self):
        return {"map_id": self.map_id, "in_game": self.in_game}

    def location(self):
        return "Pallet Town"

    def __init__(self, map_id=0, in_game=True):
        self.map_id, self.in_game, self.saves, self.restarts = map_id, in_game, 0, []
        self.x = self.badges = self.exp = 0

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
    runner.game.viewer = SimpleNamespace(panel="")
    assert runner.loop("url") == "claimed"
    assert "claimed" in runner.game.viewer.panel.splitlines()[0]
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
        memory.event("action", WALK | {"outcome": "walk: blocked after 0 step(s)"}, session.number)
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
    assert "Recent play sessions (actions, milestones, cost): session 1: 2, 0, $" in briefs[1]
    assert [e["data"]["actions"] for e in memory.events("session_end")] == [2, 2]


def test_staged_tools_become_active_at_the_next_session_after_a_checkpoint(tmp_path):
    runner, briefs, _ = make_runner(tmp_path, [(SessionResult("completed"), None)])
    spec = {"name": "step", "description": "walk right", "params": {"type": "object"}, "sha256": "0" * 64}
    runner.game.registry.define("tool", spec, "rom", None, staged=True)
    assert runner.loop("url", max_sessions=1) == "max_sessions"
    assert runner.game.registry.get("step", "tool")["status"] == "active"
    assert "step version 1 (tool) is now active" in briefs[0]
    assert runner.memory.events("checkpoint")[0]["data"]["reason"] == "before activating validated tools"


class FakeBench:
    def __init__(self, run_dir, request, live, game_class, checkpoints, max_validations):
        self.live, self.request, self.reports, self.closed = live, request, [], False

    def ws_list(self):
        return "(empty)"

    def close(self):
        self.closed = True


def test_a_development_request_runs_a_develop_session_within_the_policy(tmp_path):
    results = [(SessionResult("completed"), "walking wastes actions")] + [(SessionResult("completed"), None)] * 3
    runner, briefs, _ = make_runner(tmp_path, results)
    runner.bench_class, runner.dev_attempts, runner.server.dev_url = FakeBench, 1, "http://127.0.0.1:2/mcp"
    replay, specs, benches = runner.provider, [], []

    def scripted(spec, on_event):
        specs.append(spec)
        session = runner.server.sessions[spec.token]
        if session.mode == "develop":
            benches.append(runner.server.bench)
            runner.server.bench.reports.append({"passed": True, "version": 1})
            session.summary = "built step"
        elif session.number in (1, 3):
            action = runner.memory.event("action", WALK | {"outcome": "walk: blocked after 0 step(s)"}, session.number)
            ask = {"tool": "step", "evidence": [action], "problem": "p", "expected_benefit": "b", "proposal": "q"}
            session.request = runner.memory.event("development_requested", ask, session.number)
        return replay(spec, on_event)

    runner.provider = scripted
    assert runner.loop("http://127.0.0.1:1/mcp", max_sessions=4) == "max_sessions"
    develop_spec = specs[1]
    assert develop_spec.mcp_url == "http://127.0.0.1:2/mcp" and develop_spec.system_prompt_file.name == "develop.md"
    assert "Problem: p" in briefs[1] and "walk: blocked after 0 step(s)" in briefs[1] and "attempt 1 of 1" in briefs[1]
    assert benches[0].closed and runner.server.bench is None
    assert "Development session 2 for step (attempt 1): version 1 validated. Its summary: built step" in briefs[2]
    assert "walking wastes actions" in briefs[2]  # the last play summary, not the develop session's
    assert "request for step did not run: step already had 1 development session(s)" in briefs[3]
    cycle = runner.memory.events("development_cycle")[0]["data"]
    assert (cycle["staged"], cycle["validations"]) == (1, 1)
    assert [e["data"].get("mode", "play") for e in runner.memory.events("session_end", limit=-1)] == [
        "play",
        "develop",
        "play",
        "play",
    ]


def test_a_request_left_by_the_session_limit_runs_on_resume(tmp_path):
    runner, _, _ = make_runner(tmp_path, [(SessionResult("completed"), None)] * 2)
    runner.bench_class, runner.dev_attempts, runner.server.dev_url = FakeBench, 1, "http://127.0.0.1:2/mcp"
    replay, modes = runner.provider, []

    def scripted(spec, on_event):
        session = runner.server.sessions[spec.token]
        modes.append(session.mode)
        if session.mode == "play" and not runner.memory.events("development_requested"):
            ask = {"tool": "step", "evidence": [], "problem": "p", "expected_benefit": "b", "proposal": "q"}
            session.request = runner.memory.event("development_requested", ask, session.number)
        return replay(spec, on_event)

    runner.provider = scripted
    assert runner.loop("http://127.0.0.1:1/mcp", max_sessions=1) == "max_sessions" and modes == ["play"]
    assert runner.loop("http://127.0.0.1:1/mcp", max_sessions=1) == "max_sessions" and modes == ["play", "develop"]
    assert len(runner.memory.events("development_cycle")) == 1


def test_a_run_that_succeeded_during_play_skips_its_development_request(tmp_path):
    runner, briefs, _ = make_runner(tmp_path, [(SessionResult("completed"), None)])
    runner.bench_class, runner.dev_attempts, runner.server.dev_url = FakeBench, 1, "http://127.0.0.1:2/mcp"
    replay, modes = runner.provider, []

    def scripted(spec, on_event):
        session = runner.server.sessions[spec.token]
        modes.append(session.mode)
        runner.game.map_id = HALL_OF_FAME  # the authoritative check now holds
        ask = {"tool": "step", "evidence": [], "problem": "p", "expected_benefit": "b", "proposal": "q"}
        session.request = runner.memory.event("development_requested", ask, session.number)
        return replay(spec, on_event)

    runner.provider = scripted
    assert runner.loop("http://127.0.0.1:1/mcp") == "complete" and len(briefs) == 1 and modes == ["play"]


def test_milestones_and_a_stuck_run_that_restores_then_pauses(tmp_path):
    runner, briefs, _ = make_runner(tmp_path, [(SessionResult("completed"), None)] * 3)
    game, replay = runner.game, runner.provider
    notices = []

    def acting(spec, on_event):
        session = runner.server.sessions[spec.token]
        if session.number == 1:
            game.map_id = 1  # a first visit
            runner._after_action()
            game.badges = 1
            runner._after_action()
        actions = 0
        while not session.ended and actions < 4 * stuck.STEP:  # pacing between two tiles: no progress
            game.x = 1 - game.x
            climbed, notice = runner._after_action()
            actions += 1
            if notice:
                notices.append(notice)
            session.ended = climbed >= 3
        return replay(spec, on_event)

    runner.provider = acting
    assert runner.loop("url") == "stuck"
    milestones = [e["data"]["text"] for e in runner.memory.events("milestone", limit=-1)]
    assert milestones[-2:] == ["first visit to Pallet Town", "badge 1"]
    assert "badge 1" in [e["data"]["reason"] for e in runner.memory.events("checkpoint", limit=-1)]
    assert [int(n.split("stuck level ")[1][0]) for n in notices] == [1, 2, 3, 4, 5]
    assert "Recent milestones: first visit to Pallet Town (session 1); badge 1 (session 1)" in briefs[1]
    assert "Stuck: no progress in your last 120 actions; the stuck ladder is at level 3 of 5" in briefs[1]
    restored = runner.memory.events("restore")[0]["data"]
    assert restored["undone_actions"] == 0 and game.loaded  # actions events come from the server, not this fake
    assert "the stuck ladder is at level 4 of 5" in briefs[2] and "The game was restored" in briefs[2]
    assert len(briefs) == 3


def test_a_stuck_restore_ignores_checkpoints_of_an_earlier_ladder(tmp_path):
    runner, _, _ = make_runner(tmp_path, [(SessionResult("completed"), None)])
    runner._after_action()
    runner.checkpoint("old ladder")  # as if saved before a resume: same score, another ladder instance
    best = runner.ladder.best
    runner.ladder = stuck.Ladder(runner.game.adapter)
    runner.ladder.best = best
    assert runner._restore_progress() is False
    runner.checkpoint("new ladder")
    assert runner._restore_progress() is True


def test_a_badge_on_the_first_action_is_a_milestone_and_its_checkpoint_carries_the_badge_score(tmp_path):
    runner, _, _ = make_runner(tmp_path, [(SessionResult("completed"), None)])
    runner.game.badges = 1  # earned by the very first action after the start
    runner._after_action()
    assert [e["data"]["text"] for e in runner.memory.events("milestone", limit=-1)][-1] == "badge 1"
    checkpoint = next(e for e in runner.memory.events("checkpoint", limit=-1) if e["data"]["reason"] == "badge 1")
    assert checkpoint["data"]["best"] == runner.ladder.best


def test_success_is_judged_before_a_stuck_restore(tmp_path):
    runner, _, _ = make_runner(tmp_path, [(SessionResult("completed"), None)])
    game, replay = runner.game, runner.provider

    def acting(spec, on_event):
        for _ in range(4 * stuck.STEP):
            game.x = 1 - game.x
            runner._after_action()
        game.map_id = HALL_OF_FAME  # the check holds, but the ladder never saw progress
        return replay(spec, on_event)

    runner.provider = acting
    runner.game.viewer = SimpleNamespace(panel="")
    assert runner.loop("url") == "complete" and not runner.memory.events("restore")
    assert "complete" in runner.game.viewer.panel.splitlines()[0]


def test_the_viewer_panel_shows_a_stuck_level_when_it_is_reached(tmp_path):
    runner, _, _ = make_runner(tmp_path, [(SessionResult("completed"), None)])
    runner.game.viewer = SimpleNamespace(panel="")
    notice = ""
    for _ in range(4 * stuck.STEP):
        runner.game.x = 1 - runner.game.x
        _, notice = runner._after_action()
        if notice:
            break
    assert notice and "level 1" in runner.game.viewer.panel


def test_the_viewer_panel_follows_the_run(tmp_path):
    runner, _, _ = make_runner(tmp_path, [(SessionResult("completed"), "found the lab")])
    runner.game.viewer = SimpleNamespace(panel="")
    replay, shown = runner.provider, []

    def watching(spec, on_event):
        shown.append(runner.game.viewer.panel)
        return replay(spec, on_event)

    runner.provider = watching
    runner.loop("url", max_sessions=1)
    assert "now session 1 (play)" in shown[0]
    assert "Sessions: 1 play, 0 develop; 3 actions" in runner.game.viewer.panel


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
    assert len(runner.memory.events("limit_wait")) == 1
    assert "walked north" in briefs[2]


def test_an_interrupted_limit_wait_records_the_time_actually_waited(tmp_path):
    def interrupted(seconds):
        time.sleep(0.05)
        raise KeyboardInterrupt

    runner, _, _ = make_runner(tmp_path, [(SessionResult("limit", resets_at=int(time.time()) + 3600), None)])
    runner.sleep = interrupted
    with pytest.raises(KeyboardInterrupt):
        runner.loop("url", max_sessions=1)
    (wait,) = runner.memory.events("limit_wait")
    assert 0.04 < wait["data"]["seconds"] < 5


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


def test_tools_on_the_rom(game, tmp_path):
    gen1, emu, bedroom = game
    emu.load_state(bedroom)
    memory = Memory(tmp_path / "memory.db", "run")
    memory.open_run({"objective": "x", "rom_sha1": "red", "family": "pokemon-gen1"})
    live = Game(emu, gen1, registry=Registry(memory), library=tmp_path / "library", tkv=tmp_path / "tkv.json")
    x = live.observations()["x"]
    source = (
        "def run(ctx):\n    ctx.call('walk', direction='right')\n    ctx.log('moved')\n    return ctx.observe('x')\n"
    )
    names = set(OBSERVATIONS)
    install(
        live.registry,
        live.library,
        {"name": "step", "description": "d", "effects": ["position"]},
        source,
        "rom",
        1,
        names,
        set(),
    )
    text, _, _, error = live.run_tool("step", {})
    assert error is None and text.startswith(f"run_tool step: returned {x + 1}\nIts log:\n  step: moved")
    install(live.registry, live.library, {"name": "sneaky", "description": "d"}, source, "rom", 1, names, set())
    text, _, _, error = live.run_tool("sneaky", {})
    assert "changed position, which its effects do not declare" in error and "Now: Reds House 2F" in text
    assert live.observations()["x"] == x + 2  # a failed call keeps its effect on the game


def test_a_tool_validates_on_the_rom(game, tmp_path):
    gen1, emu, bedroom = game
    memory = Memory(tmp_path / "memory.db", "run")
    memory.open_run({"objective": "x", "rom_sha1": "red", "family": "pokemon-gen1"})
    trial = Game(emu, gen1, registry=Registry(memory), library=tmp_path / "library", tkv=tmp_path / "tkv.json")
    files = {
        "tool.py": "def run(ctx):\n    ctx.call('walk', direction='right')\n    return ctx.observe('x')\n",
        "test_tool.py": "def test_moves(ctx):\n    ctx.load_fixture('bedroom')\n"
        "    x = ctx.observe('x')\n    assert ctx.call('step') == x + 1\n",
    }
    manifest = {"name": "step", "description": "one step right", "effects": ["position"]}
    names = set(OBSERVATIONS)
    report = validate(trial, manifest, files, {"bedroom": bedroom}, [("live", bedroom)], "rom", 1, names, set())
    assert report["passed"], report
    assert [s["step"] for s in report["steps"]][-2:] == ["contract on live", "contract runs"]


def test_each_contract_run_starts_with_empty_scratch_stores(game, tmp_path):
    gen1, emu, bedroom = game
    memory = Memory(tmp_path / "memory.db", "run")
    memory.open_run({"objective": "x", "rom_sha1": "red", "family": "pokemon-gen1"})
    trial = Game(emu, gen1, registry=Registry(memory), library=tmp_path / "library", tkv=tmp_path / "tkv.json")
    files = {
        "tool.py": "def run(ctx):\n    seen = ctx.kv.get('seen')\n    ctx.kv.set('seen', True)\n"
        "    assert seen is None, 'a store leaked from an earlier run'\n",
        "test_tool.py": "def test_ok(ctx):\n    ctx.call('step')\n",
    }
    manifest = {"name": "step", "description": "d", "examples": [{}, {}]}
    report = validate(trial, manifest, files, {}, [("live", bedroom)], "rom", 1, set(OBSERVATIONS), set())
    assert report["passed"], report


def test_the_development_bench_leaves_the_live_game_alone_and_stages_a_tool(game, rom_path, tmp_path):
    gen1, _, bedroom = game
    memory = Memory(tmp_path / "memory.db", "run")
    memory.open_run({"objective": "x", "rom_sha1": "red", "family": "pokemon-gen1"})

    def start():
        return Emulator(core_path(CORE), rom_path, options=CORE_OPTIONS)

    live = Game(start(), gen1, start=start, registry=Registry(memory), library=tmp_path / "library")
    live.load_state(bedroom)
    x = live.observations()["x"]
    bench = Bench(tmp_path / "run", {"tool": "step"}, live, Game, lambda: [])
    try:
        bench.snapshot("before")
        bench.game.act("walk", direction="right")
        assert bench.game.observations()["x"] == x + 1 and live.observations()["x"] == x
        assert "byte(s) differ" in bench.diff("before") and "0xd362" in bench.search(x + 1)  # wXCoord
        bench.fixture_save("moved")
        bench.ws_write(
            "manifest.json", json.dumps({"name": "step", "description": "one step right", "effects": ["position"]})
        )
        bench.ws_write(
            "tool.py", "def run(ctx):\n    ctx.call('walk', direction='right')\n    return ctx.observe('x')\n"
        )
        bench.ws_write(
            "test_tool.py",
            "def test_moves(ctx):\n    ctx.load_fixture('moved')\n    x = ctx.observe('x')\n"
            "    assert ctx.call('step') == x + 1\n",
        )
        names = set(OBSERVATIONS)
        report = bench.validate("rom", 2, names, names)
        assert report["passed"], report
        assert "contract on fixture moved" in [s["step"] for s in report["steps"]]
        assert [c["name"] for c in live.registry.staged()] == ["step"] and live.observations()["x"] == x
        bench.load()
        assert bench.game.observations()["x"] == x
    finally:
        bench.close()
        live.emu.close()


def test_the_ladder_reads_progress_from_the_rom(game):
    gen1, emu, bedroom = game
    emu.load_state(bedroom)
    live, ladder = Game(emu, gen1), stuck.Ladder(gen1)
    assert ladder.observe(live.progress_state(), 1) == 0 and ladder.best
    live.act("walk", direction="right")
    first = ladder.best
    assert ladder.observe(live.progress_state(), 1) == 0 and ladder.best > first  # a new tile
    live.act("walk", direction="left")
    assert ladder.observe(live.progress_state(), 1) == 0 and (ladder.best, ladder.since) == (first + 1, 1)


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
    tkv = run_dir / "tkv.json"  # the tools' timeline store
    tkv.write_text('{"router": {"parcel": "at the Mart"}}')
    runner.checkpoint("session 1")
    checkpoint = restorable(run_dir, memory)[-1]["data"]["file"]
    first = memory.timeline()
    for _ in range(3):
        runner.server.after_action()
        memory.event("action", WALK | {"outcome": "walk: moved"}, 2)
    delivered = memory.add_knowledge("progress", "Parcel", "delivered", scope="run")
    notes.write_text("go to Viridian\nOak gave me the Pokédex\n")
    progress.write_text(json.dumps({"sessions": 4, "summary": "delivered the parcel", "next_focus": "Route 2"}))
    tkv.write_text('{"router": {"parcel": "delivered"}}')
    runner.checkpoint("session 4")

    memory.set_status("complete", "done")
    done = restore(run_dir, memory, checkpoint)
    assert memory.status()[0] == "running"
    assert done["undone_actions"] == 3 and done["stale"] == [delivered]
    assert latest_state(run_dir) == b"state 1" and notes.read_text() == "go to Viridian\nthe Mart has the parcel\n"
    assert json.loads(progress.read_text()) == {"sessions": 4, "summary": "left Pallet", "next_focus": "the Mart"}
    assert json.loads(tkv.read_text()) == {"router": {"parcel": "at the Mart"}}
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


def test_a_restart_also_restores_the_timeline_store(tmp_path):
    runner, _, _ = make_runner(tmp_path, [])
    tkv = runner.run_dir / "tkv.json"
    runner.server.after_action()
    runner.run_dir.joinpath("tkv.json").write_text('{"t": {"done": true}}')  # a tool wrote it, then the action failed
    assert runner.recover("worker exited") and not tkv.exists()
    tkv.write_text('{"t": {"step": 1}}')
    runner.server.after_action()
    tkv.write_text('{"t": {"step": 2}}')
    assert runner.recover("worker exited") and json.loads(tkv.read_text()) == {"t": {"step": 1}}
