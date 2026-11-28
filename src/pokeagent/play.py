"""Runs: a run directory, the live game, and the loop of Claude sessions that plays until the objective's check
passes, a limit is reached or a blocker stops it.

A run directory (local only) holds `run.json` (objective, checks, ROM identity; never rewritten), `state.json` (session
count, last summary and focus), `notes.md`, `play.log`, `recovery.state` (the game after the last action),
`checkpoints/*.state` (each with its notes and progress), the raw session streams and the web log. The data directory's
`memory.db` holds the runs' status, success checks, timelines, events and knowledge (see memory.py).
"""

import contextlib
import fcntl
import hashlib
import io
import json
import os
import random
import shutil
import signal
import threading
import time
from functools import partial
from pathlib import Path

from PIL import Image

from pokeagent import capabilities, develop, extensions, objectives, stats, stuck
from pokeagent.agents.claude_cli import ProviderError, SessionResult, SessionSpec, preflight, run_session
from pokeagent.capabilities import Registry
from pokeagent.emulator.client import Emulator, EmulatorError
from pokeagent.emulator.cores import core_path
from pokeagent.games.pokemon_gen1 import (
    CORE,
    CORE_OPTIONS,
    DEFAULT_CHECK,
    DEFAULT_OBJECTIVE,
    FAMILY,
    GLITCHES,
    OBSERVATIONS,
    SIDE_QUESTS,
    STARTERS,
    TEAM_TYPES,
    Gen1,
)
from pokeagent.games.pokemon_gen1 import decode as decode_text
from pokeagent.memory import Memory, describe_knowledge
from pokeagent.server import GameServer

PROMPTS = Path(__file__).with_name("prompts")
BUTTONS = ("a", "b", "start", "select", "up", "down", "left", "right")
DIRECTIONS = ("up", "down", "left", "right")
SETTLE_FRAMES = 20  # map events (Oak in Pallet Town) start a few frames after a walk ends
KEEP_CHECKPOINTS = 10
CONTEXT_FILES = ("notes.md", "state.json", "tkv.json")
CHECKPOINT_EVERY = 25  # actions
MAX_FAILURES = 5
MAX_RESTARTS = 3  # emulator restarts per play
KNOWLEDGE_HITS = 5
IDENTITY = ("rom_sha1", "core", "core_options")  # what a checkpoint must share with its run to be restored
LEGEND = "@ you, S person or object, W warp/door, . walkable, , grass, v < > ^ ledges, # blocked"
STYLES = ("natural", "direct")
NICKNAME_THEMES = (
    "gemstones", "spices and herbs", "rivers", "stars and constellations", "heroes of old legends", "cheeses",
    "weather", "trees", "musical terms", "painters", "desserts", "mountains", "sea creatures", "colours",
)  # fmt: skip


def default_data_dir() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "pokeagent"


def open_memory(run_dir: Path, data_dir: Path) -> Memory:
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    memory = Memory(data_dir / "memory.db", run_dir.name)
    memory.open_run(json.loads((run_dir / "run.json").read_text()))
    return memory


@contextlib.contextmanager
def run_lock(run_dir: Path):
    """Only one pokeagent process plays or restores a run at a time."""
    with (run_dir / ".lock").open("w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError(f"run {run_dir.name} is in use by another pokeagent process") from None
        yield


def write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def write_atomic_pair(first: Path, first_data: bytes, second: Path, second_data: bytes | None) -> None:
    """Both files are written before either is replaced, so a failed write leaves both as they were.
    No `second_data` removes `second`."""
    tmps = [(first, first.with_suffix(first.suffix + ".tmp"), first_data)]
    if second_data is not None:
        tmps.append((second, second.with_suffix(second.suffix + ".tmp"), second_data))
    for _, tmp, data in tmps:
        tmp.write_bytes(data)
    for path, tmp, _ in tmps:
        tmp.replace(path)
    if second_data is None:
        second.unlink(missing_ok=True)


class Game:
    """The live emulator and adapter. One lock serialises every call. The adapter drives this object as its
    emulator, so `run` can pace emulation (`speed` x real time) and feed the viewer. `start` makes a new emulator
    for `restart`; `registry` holds the capabilities the agent defined, `library` their code and `tkv` the path of
    the tools' timeline-bound store."""

    def __init__(
        self,
        emu: Emulator,
        adapter: Gen1,
        speed: float | None = None,
        viewer=None,
        start=None,
        registry=None,
        library: Path | None = None,
        tkv: Path | None = None,
    ):
        self.emu, self.adapter, self.speed, self.viewer, self.start = emu, adapter, speed, viewer, start
        self.registry: Registry | None = registry
        self.library, self.tkv = library, tkv
        self.frames = 0  # emulated frames since start, for tool budgets
        self.presses = 0  # `run` calls with a button held, for the tools' input budget
        self.lock = threading.Lock()
        self.fps = emu.info["fps"]
        self._shown = 0.0

    def read(self, addr: int, n: int) -> bytes:
        return self.emu.read(addr, n)

    def run(self, buttons=(), frames: int = 1) -> None:
        self.emu.run(buttons, frames)
        self.frames += frames
        self.presses += bool(buttons)
        if self.speed:
            time.sleep(frames / (self.fps * self.speed))  # ponytail: ignores emulation time; fine at watch speeds
        if self.viewer and time.monotonic() - self._shown > 1 / 30:
            self._shown = time.monotonic()
            self.viewer.frame = self.emu.frame()

    def png(self, scale: int = 3) -> bytes:
        frame = self.emu.frame()
        out = io.BytesIO()
        frame.resize((frame.width * scale, frame.height * scale), Image.Resampling.NEAREST).save(out, "PNG")
        return out.getvalue()

    def save_state(self) -> bytes:
        with self.lock:
            return self.emu.save_state()

    def load_state(self, state: bytes) -> None:
        with self.lock:
            self.emu.load_state(state)

    def restart(self, state: bytes | None) -> None:
        """Replace the emulator (after its worker failed) with a new one at `state` (None: power-on)."""
        with self.lock:
            with contextlib.suppress(EmulatorError, OSError):
                self.emu.close()
            self.emu = self.start()
            if state:
                self.emu.load_state(state)

    def progress_state(self) -> dict:
        """The adapter's full state dict, for milestones and the stuck ladder."""
        with self.lock:
            return self.adapter.state(self)

    def state_text(self) -> str:
        with self.lock:
            return self._text(self.adapter.state(self), detail=True)

    def _text(self, state: dict, detail: bool = False) -> str:
        text = self.adapter.state_text(state, detail)
        if shown := self._custom(shown_only=True):
            text += "\nYour observations: " + ", ".join(f"{name}={value!r}" for name, value in shown.items())
        return text

    def observations(self) -> dict:
        with self.lock:
            return self.adapter.observations(self.adapter.state(self))

    def location(self) -> str:
        """The current map's name, or "" before a game is in progress."""
        with self.lock:
            state = self.adapter.state(self)
            return state["map"] if state["in_game"] else ""

    def full_state(self) -> tuple[str, bytes]:
        with self.lock:
            state = self.adapter.state(self)
            text = self._text(state, detail=True)
            if state["in_game"]:
                grid = self.adapter.grid(self)
                text += f"\n\nWhole map ({LEGEND}):\n{grid.render((state['x'], state['y']))}"
            return text, self.png()

    def _custom(self, shown_only: bool = False) -> dict:
        """The values of the run's active observations (call under the lock)."""
        if self.registry is None:
            return {}
        return {
            cap["name"]: self._decode(cap["spec"])
            for cap in self.registry.active("observation")
            if cap["spec"]["show"] or not shown_only
        }

    def _decode(self, spec: dict):
        return capabilities.decode(spec, self.emu.read(spec["address"], spec["length"]), decode_text)

    def _observations(self) -> dict:
        """Builtin and the run's own observations, for macro conditions (call under the lock)."""
        return self.adapter.observations(self.adapter.state(self)) | self._custom()

    def peek(self, address: int, length: int) -> bytes:
        """Read RAM (RAM regions only; ROM and unmapped addresses are refused)."""
        with self.lock:
            return self.emu.read(address, length)

    def try_observation(self, spec: dict):
        """Read and decode a new observation now, before it is defined."""
        with self.lock:
            return self._decode(spec)

    def act(self, name: str, **args) -> tuple[str, bytes | None]:
        """Run one builtin action; return a compact report and a screenshot when the screen mode changed."""
        with self.lock:
            before = self.adapter.state(self)
            return self._report(name, self._do(name, args), before)

    def run_macro(self, name: str, args: dict) -> tuple[str, bytes | None, dict, str | None]:
        """Run a macro as one action: (report, screenshot, the capability, error or None). A macro that fails part
        way still reports the state it left behind."""
        with self.lock:
            macro = self.registry.get(name, "macro")
            capabilities.check_args(macro["spec"], args)  # a bad call is refused, not counted against the macro
            before = self.adapter.state(self)
            try:
                outcomes = capabilities.execute(
                    macro["spec"], args, self._do, self._observations, lambda n: self.registry.get(n, "macro")["spec"]
                )
                error, outcome = (
                    None,
                    f"done after {len(outcomes)} action(s)" + (f"; the last: {outcomes[-1]}" if outcomes else ""),
                )
            except ValueError as e:  # CapabilityError, a refused builtin action or a refused request
                error, outcome = str(e), f"failed: {e}"
            text, png = self._report(f"run_macro {name}", outcome, before)
            return text, png, macro, error

    def run_tool(self, name: str, args: dict) -> tuple[str, bytes | None, dict, str | None]:
        """Run a code extension as one action, like `run_macro`. A refused call (arguments, preconditions) raises."""
        with self.lock:
            tool = self.registry.get(name, "tool")
            before = self.adapter.state(self)
            budget = extensions.Budget.start(self)
            try:
                result = extensions.Runner(self).call(tool, args, budget)
                error, outcome = None, f"returned {json.dumps(result, default=str)[:1000]}"
            except extensions.Refused:
                raise
            except ValueError as e:  # a failure or contract violation
                error, outcome = str(e), f"failed: {e}"
            if budget.logs:
                outcome += "\nIts log:\n" + "\n".join("  " + line for line in budget.logs)
            text, png = self._report(f"run_tool {name}", outcome, before)
            return text, png, tool, error

    def _report(self, name: str, outcome: str, before: dict) -> tuple[str, bytes | None]:
        after = self.adapter.state(self)
        old, new = self.adapter.fingerprint(before), self.adapter.fingerprint(after)
        changed = [group for group in new if new[group] != old[group]]
        lines = [f"{name}: {outcome}", f"Changed: {', '.join(changed) or 'nothing'}"]
        if not after["in_game"] or set(changed) & {"party", "bag", "money", "battle"}:
            lines.append(self._text(after))
        else:
            screen = after["screen"]
            lines.append(
                f"Now: {after['map']} ({after['x']},{after['y']}) facing {after['facing']}; screen {screen['mode']}"
                + (" (yes/no prompt)" if screen["yes_no"] else "")
            )
            if after["screen_text"]:
                lines.append("Screen text:\n" + "\n".join("  " + line for line in after["screen_text"]))
        if after["in_game"] and after["screen"]["mode"] == "overworld":
            crop = self.adapter.grid(self).render((after["x"], after["y"]), radius=4)
            lines.append(f"Nearby ({LEGEND}):\n{crop}")
        if self.viewer:
            self.viewer.status = f"{name}: {outcome} | {after['map']} ({after['x']},{after['y']})"
        mode_changed = before["screen"]["mode"] != after["screen"]["mode"]
        return "\n".join(lines), self.png() if mode_changed else None

    def _do(self, name: str, args: dict) -> str:
        """One builtin action. Also the step runner of macros, whose steps may leave out optional arguments."""
        adapter = self.adapter
        if name in ("walk", "walk_to") and not adapter.state(self)["in_game"]:
            raise ValueError("no game is in progress yet; use press and advance_text until one is")
        match name:
            case "press":
                button, times = args.get("button"), args.get("times", 1)
                if not isinstance(button, str) or button not in BUTTONS:
                    raise ValueError(f"button must be one of {', '.join(BUTTONS)}")
                if not isinstance(times, int) or not 1 <= times <= 10:
                    raise ValueError("times must be 1..10")
                for _ in range(times):
                    adapter.press(self, button)
                return f"pressed {button} x{times}"
            case "walk":
                direction, steps = args.get("direction"), args.get("steps", 1)
                if not isinstance(direction, str) or direction not in DIRECTIONS:
                    raise ValueError(f"direction must be one of {', '.join(DIRECTIONS)}")
                if not isinstance(steps, int) or not 1 <= steps <= 20:
                    raise ValueError("steps must be 1..20")
                result = adapter.walk(self, direction, steps)
            case "walk_to":
                x, y = args.get("x"), args.get("y")
                if not isinstance(x, int) or not isinstance(y, int):
                    raise ValueError("walk_to needs whole-number x and y")
                result = adapter.walk_to(self, x, y)
            case "advance_text":
                result = adapter.advance_text(self)
                return f"{result['outcome']} after {result['presses']} A press(es)"
            case _:
                raise ValueError(f"unknown action {name}")
        self.run([], SETTLE_FRAMES)
        return f"{result['outcome']} after {result['steps']} step(s)"


def new_run(
    rom: Path | None,
    objective: str | None,
    data_dir: Path,
    success: dict | None = None,
    style: str = "natural",
    glitches: bool = False,
    source: Path | None = None,
    sav: Path | None = None,
) -> Path:
    """Create a run directory for a supported ROM, or for `source`'s ROM starting from that run's latest game. With
    `sav`, the ROM's cartridge starts with that in-game save (read, never changed), so the game offers CONTINUE. Only
    the adapter's default objective gets its trusted check; `success` is the user's own check. The natural `style`
    draws the run's variety here, once; `glitches` allows the game's known glitches."""
    if style not in STYLES:
        raise ValueError(f"unknown style {style!r}; choose from {', '.join(STYLES)}")
    start = None
    if source:
        source_meta, rom, _ = _run_rom(source)
        if (source_meta["core"], source_meta["core_options"]) != (CORE, CORE_OPTIONS):
            raise ValueError(f"run {source.name} used another core or core options; start it from power-on instead")
        if (start := latest_state(source)) is None:
            raise ValueError(f"run {source.name} has no game to start from yet")
    data = rom.read_bytes()
    adapter = Gen1(data)  # refuses unsupported ROMs
    if sav:
        if not adapter.save_complete(saved := sav.read_bytes()):
            raise ValueError(f"{sav} is not a whole Red/Blue save (32 KB with the game's checksum)")
        with Emulator(core_path(CORE), rom, options=CORE_OPTIONS, battery=sav) as emu:
            if emu.save_ram() != saved:
                raise ValueError(f"the cartridge did not take {sav}")
            start = emu.save_state()  # power-on with the save in the cartridge
    if success is not None:
        objectives.validate(success, [name for name in OBSERVATIONS if name != "in_game"])
    (data_dir / "runs").mkdir(parents=True, exist_ok=True, mode=0o700)
    earlier = [json.loads(path.read_text()) for path in (data_dir / "runs").glob("*/run.json")]
    run_dir = data_dir / "runs" / time.strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(mode=0o700)
    meta = {
        "objective": objective or DEFAULT_OBJECTIVE,
        "check": None if objective else DEFAULT_CHECK,
        "success": success,
        "game": adapter.title,
        "family": FAMILY,
        "rom": str(rom.resolve()),
        "rom_sha1": hashlib.sha1(data).hexdigest(),
        "core": CORE,
        "core_options": CORE_OPTIONS,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "style": style,
        "glitches": glitches,
        "variety": draw_variety(adapter, earlier) if style == "natural" else None,
        "from": f"run {source.name}" if source else f"save file {sav.name}" if sav else None,
    }
    (run_dir / "run.json").write_text(json.dumps(meta, indent=1) + "\n")
    if start:
        write_atomic(run_dir / "recovery.state", start)
    return run_dir


def draw_variety(adapter: Gen1, earlier: list[dict]) -> dict:
    """A natural run's draw: the starter the fewest `earlier` runs drew, three team types besides the starter's, a
    nickname theme and three side quests."""
    drawn = [meta["variety"]["starter"] for meta in earlier if meta["variety"]]
    starters = {adapter.species_name(index): types for index, types in STARTERS.items()}
    fewest = min(drawn.count(name) for name in starters)
    starter = random.choice([name for name in starters if drawn.count(name) == fewest])
    types = random.sample([t for t in TEAM_TYPES if t not in starters[starter]], 3)
    return {
        "starter": starter,
        "team_types": [adapter.types[t] for t in types],
        "nicknames": random.choice(NICKNAME_THEMES),
        "side_quests": random.sample(SIDE_QUESTS, 3),
    }


def style_text(meta: dict, party: list[dict]) -> list[str]:
    """The brief's play style lines: the natural style's house rules with the run's variety and team facts, and the
    glitch allowance. None for a direct run without glitches."""
    lines = []
    if variety := meta["variety"]:
        types = variety["team_types"]
        lines += [
            "Play style: natural. Play the way a person enjoys the game rather than by the fastest route; where the"
            " two conflict, the objective comes first.",
            "- Catch each species you do not own yet when you meet it, as far as your Poké Balls and money allow.",
            "- Build a balanced team of up to six in which no two members share a type, not the team you would always"
            f" pick: besides your starter, this run's team has Pokémon of the types {', '.join(types[:-1])} and"
            f" {types[-1]}.",
            "- Train the team evenly: rotate who battles so nobody runs far ahead of the others.",
            "- Use your items: healing and status items, TMs and HMs on members who benefit, evolution stones,"
            " PP Ups, Rare Candies.",
            "- Leave Pokémon in the PC when your team has no room or no use for them, and swap them back when they"
            " fit.",
            f"- Nickname every Pokémon you take into your team; this run's names: {variety['nicknames']}.",
            f"- If you still have a starter to choose, choose {variety['starter']}.",
            "- Side quests for this run, to take up when they fit: " + " ".join(variety["side_quests"]),
        ]
        if party:
            levels = [p["level"] for p in party]
            members: dict[str, list[str]] = {}
            for p in party:
                for kind in p["types"]:
                    members.setdefault(kind, []).append(p["name"])
            shared = [f"{kind} ({', '.join(names)})" for kind, names in members.items() if len(names) > 1]
            plain = [p["name"] for p in party if p["name"] == p["species"]]
            lines.append(
                f"Your team now: levels {min(levels)} to {max(levels)}"
                + (f"; sharing a type: {'; '.join(shared)}" if shared else "")
                + (f"; no nickname yet: {', '.join(plain)}" if plain else "")
                + "."
            )
    if meta["glitches"]:
        lines.append(GLITCHES)
    return lines


class Runner:
    """The session loop. `provider` runs one Claude session (`run_session` in production, a fake in tests); the
    server's memory holds the run's checks, events and knowledge."""

    def __init__(
        self,
        run_dir: Path,
        game,
        server: GameServer,
        provider=run_session,
        sleep=time.sleep,
        budget: int = 100,
        session_timeout: float = 3600,
        model: str | None = None,
        effort: str | None = None,
        dev_share: float = 0.3,
        dev_attempts: int = 2,
        dev_timeout: float = 45 * 60,
        dev_validations: int = 10,
    ):
        run_dir = run_dir.resolve()  # the Claude child runs with cwd=run_dir, so session paths must be absolute
        self.run_dir, self.game, self.server, self.provider, self.sleep = run_dir, game, server, provider, sleep
        self.budget, self.session_timeout, self.model, self.effort = budget, session_timeout, model, effort
        self.dev_share, self.dev_attempts, self.dev_timeout = dev_share, dev_attempts, dev_timeout
        self.dev_validations, self.bench_class = dev_validations, develop.Bench
        self.meta = json.loads((run_dir / "run.json").read_text())
        self.memory: Memory = server.memory
        self._actions = self.restarts = 0
        self._after_lock = threading.Lock()  # a save and its write stay together, and a restart waits for them
        self.ladder = stuck.Ladder(game.adapter)
        self._climbed = 0  # the highest ladder level reached in the current session
        self._maps = self._visited_maps()  # milestone state; _seen: badges and Pokédex count
        self._baseline()
        self.number: int | None = None  # the current session
        server.after_action, server.recover = self._after_action, self.recover
        (run_dir / "sessions").mkdir(exist_ok=True)
        (run_dir / "checkpoints").mkdir(exist_ok=True)

    def log(self, kind: str, text: str = "") -> None:
        line = " ".join(text.split())[:500]
        with (self.run_dir / "play.log").open("a") as f:  # ponytail: no rotation yet; add at 50 MB as planned
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {kind} {line}\n")

    def on_event(self, kind: str, data: dict) -> None:
        match kind:
            case "text":
                self.log("text", data["text"])
            case "tool_call":
                self.log("tool", f"{data['name'].removeprefix('mcp__game__')} {json.dumps(data['input'])}")
            case "tool_result" if data["error"]:
                self.log("tool_error")
            case "limit":
                self.log("limit", f"resets at {data['resets_at']}")

    def checkpoint(self, reason: str, state: bytes | None = None) -> None:
        folder = self.run_dir / "checkpoints"
        path = folder / f"{time.time_ns()}.state"
        write_atomic(path, self.game.save_state() if state is None else state)
        for name in CONTEXT_FILES:  # timeline-bound context travels with its checkpoint
            if (self.run_dir / name).exists():
                shutil.copyfile(self.run_dir / name, path.with_suffix(f".{name}"))
        for old in sorted(folder.glob("*.state"))[:-KEEP_CHECKPOINTS]:
            old.unlink()
            for name in CONTEXT_FILES:
                old.with_suffix(f".{name}").unlink(missing_ok=True)
        identity = {key: self.meta[key] for key in IDENTITY}
        # progress and ladder: the ladder's progress revision and instance, for the stuck level 4 restore
        data = {"file": path.name, "reason": reason, "progress": self.ladder.revision, "ladder": self.ladder.id}
        data |= identity
        self.memory.event("checkpoint", data)
        self.log("checkpoint", f"{path.name} ({reason})")

    def _after_action(self) -> tuple[int, str]:
        """After every completed action: the recovery state, milestones, the stuck ladder and periodic checkpoints.
        Returns the level the ladder climbed to (or 0) and the notice for the agent."""
        with self._after_lock:
            state = self.game.save_state()  # one emulator save: a second one could fail after the action is committed
            try:
                tkv = self.run_dir / "tkv.json"  # the tools' timeline store is part of the recovery snapshot
                write_atomic_pair(
                    self.run_dir / "recovery.state",
                    state,
                    self.run_dir / "recovery.tkv.json",
                    tkv.read_bytes() if tkv.exists() else None,
                )
            except OSError as e:  # the emulator is ahead of the file: report it as failed so it restarts from the file
                raise EmulatorError(f"the recovery state could not be written: {e}") from e
            self._actions += 1
            watched = self._watch(state)
            if self._actions % CHECKPOINT_EVERY == 0:
                self.checkpoint(f"{self._actions} actions", state)
            return watched

    def show(self, now: str = "") -> None:
        """Refresh the viewer's run panel, if a viewer is open."""
        if self.game.viewer:
            self.game.viewer.panel = stats.status(self.memory, self.run_dir, now)

    def _watch(self, saved: bytes) -> tuple[int, str]:
        state = self.game.progress_state()
        if not state["in_game"]:
            return 0, ""
        badge = self._milestones(state)
        climbed = self.ladder.observe(state, len(self._maps))
        if badge:  # after the ladder took the badge into its best score, so the checkpoint is tagged with it
            self.checkpoint(f"badge {state['badges']}", saved)
        if not climbed:
            return 0, ""
        self._climbed = max(self._climbed, climbed)
        self.memory.event("stuck", {"level": climbed, "since": self.ladder.since}, self.number)
        self.show(f"session {self.number} (play)")  # after the event: the panel reads the latest stuck level
        self.log("stuck", f"level {climbed} after {self.ladder.since} actions without progress")
        notice = stuck.describe(climbed, self.ladder.since)
        if hits := self.memory.search(state["map"], 3):
            notice += "\nKnowledge that may bear on this:\n" + "\n".join(describe_knowledge(e) for e in hits)
        return climbed, notice

    def _baseline(self) -> None:
        """The badges and Pokédex count of the loaded game, read before its first action."""
        state = self.game.progress_state()
        self._seen = {"badges": state["badges"], "dex_owned": state["dex_owned"]} if state["in_game"] else None

    def _milestones(self, state: dict) -> bool:
        """Record first visits to maps, new badges and new Pokédex entries as milestone events of the timeline.
        Returns whether a badge was earned."""
        found = []
        if state["map_id"] not in self._maps:
            self._maps.add(state["map_id"])
            found.append({"kind": "map", "map_id": state["map_id"], "text": f"first visit to {state['map']}"})
        seen = {"badges": state["badges"], "dex_owned": state["dex_owned"]}
        if self._seen is not None:
            if seen["badges"] > self._seen["badges"]:
                found.append({"kind": "badge", "badges": seen["badges"], "text": f"badge {seen['badges']}"})
            if seen["dex_owned"] > self._seen["dex_owned"]:
                found.append(
                    {"kind": "species", "owned": seen["dex_owned"], "text": f"{seen['dex_owned']} species owned"}
                )
        self._seen = seen
        for milestone in found:
            self.memory.event("milestone", milestone, self.number)
            self.log("milestone", milestone["text"])
        if found:
            self.show(f"session {self.number} (play)")
        return any(m["kind"] == "badge" for m in found)

    def _visited_maps(self) -> set[int]:
        history = self.memory.history()
        return {
            e["data"]["map_id"]
            for e in self.memory.events("milestone", limit=-1)
            if e["data"]["kind"] == "map" and e["created"] <= history.get(e["timeline"], -1)
        }

    def _restore_progress(self) -> bool:
        """Stuck level 4: go back to the first checkpoint saved after the last progress; False when there is none."""
        target = next(
            (
                e
                for e in restorable(self.run_dir, self.memory)
                if e["data"].get("ladder") == self.ladder.id and e["data"].get("progress") == self.ladder.revision
            ),
            None,
        )
        if target is None:
            return False
        data = restore(self.run_dir, self.memory, target["data"]["file"])
        self.game.load_state((self.run_dir / "recovery.state").read_bytes())
        self._maps = self._visited_maps()
        self._baseline()
        self.server.must_reflect = False  # the restored timeline starts a fresh ladder
        self.log("restore", f"{data['file']} (stuck level 4): {data['undone_actions']} actions undone")
        return True

    def recover(self, error: str) -> bool:
        """Restart a failed emulator at the state after the last completed action; False once restarts run out."""
        with self._after_lock:  # wait for an action's save and write, so the restart loads the newest state
            if self.restarts >= MAX_RESTARTS:
                return False
            self.restarts += 1
            self.game.restart(latest_state(self.run_dir))
            if (self.run_dir / "recovery.state").exists():  # undo a failed action's store writes with its game changes
                saved = self.run_dir / "recovery.tkv.json"
                _put(self.run_dir / "tkv.json", saved.read_bytes() if saved.exists() else None)
        self.memory.event("restart", {"error": error})
        self.log("restart", f"the emulator was restarted after: {error}")
        return True

    def brief(self, number: int, progress: dict) -> str:
        notes = self.run_dir / "notes.md"
        observations = self.game.observations()
        lines = [f"Objective: {self.meta['objective']}", *self._checks(observations), ""]
        state = self.game.progress_state()
        if style := style_text(self.meta, state["party"] if state["in_game"] else []):
            lines += [*style, ""]
        plays = [e for e in self.memory.events("session_start", limit=-1) if e["data"].get("mode") != "develop"]
        last_start = plays[-1]["id"] if plays else 0  # what happened since the last play session began
        for restored in self.memory.events("restore", after=last_start):
            lines += [restore_notice(restored["data"]), ""]
        for rejected in self.memory.events("claim_rejected", after=last_start):
            data = rejected["data"]
            lines += [
                f"The user rejected the completion claim ({data['claim']}): {data['why'] or 'no reason given'}",
                "",
            ]
        for decision in self.memory.events("development_decision", after=last_start):
            data = decision["data"]
            lines += [f"Your development request for {data['tool']} did not run: {data['reason']}.", ""]
        for cycle in self.memory.events("development_cycle", after=last_start):
            data = cycle["data"]
            outcome = f"version {data['staged']} validated" if data["staged"] else "nothing validated"
            lines += [
                f"Development session {cycle['session']} for {data['tool']} (attempt {data['attempt']}): {outcome}."
                f" Its summary: {data['summary'] or 'none'}",
                "",
            ]
        for kind in ("capability_rollback", "capability_activated"):
            lines += [
                line for event in self.memory.events(kind, after=last_start) for line in (event["data"]["note"], "")
            ]
        history = self.memory.history()
        reached = [
            e for e in self.memory.events("milestone", limit=-1) if e["created"] <= history.get(e["timeline"], -1)
        ]
        if reached:
            lines += [
                "Recent milestones: "
                + "; ".join(f"{e['data']['text']} (session {e['session']})" for e in reached[-5:]),
                "",
            ]
        if self.ladder.level:
            lines += [stuck.brief(self.ladder.level, self.ladder.since), ""]
        lines += [
            f"This is session {number}. You have {self.budget} game actions (press, walk, walk_to, advance_text,"
            " run_macro, run_tool); every other tool is free.",
            f"Last session's summary: {progress.get('summary') or 'none, this is the first session.'}",
            f"Planned focus: {progress.get('next_focus') or 'none'}",
            "",
            "Your notes:",
            (notes.read_text() if notes.exists() else "") or "(empty)",
        ]
        if hits := self.memory.search(f"{self.game.location()} {progress.get('next_focus') or ''}", KNOWLEDGE_HITS):
            lines += ["", "Knowledge that may bear on this (knowledge_search finds more):"]
            lines += [describe_knowledge(entry) for entry in hits]
        if efficiency := stats.efficiency(self.memory):
            lines += ["", *efficiency]
        if found := self.game.registry.active():
            lines += ["", "Your observations, macros and tools:"]
            lines += [capabilities.summary(self.game.registry, c) for c in found]
        return "\n".join([*lines, "", "Current game state:", self.game.state_text()])

    def _checks(self, observations: dict) -> list[str]:
        in_game = observations["in_game"]

        def now(condition: dict) -> str:
            return (
                ("holds" if objectives.holds(condition, observations) else "does not hold")
                if in_game
                else "not judged yet"
            )

        active, proposed = self.memory.checks("active"), self.memory.checks("proposed")
        lines = [
            f"Success check ({check['source']}): {objectives.describe(check['condition'])}; {now(check['condition'])}."
            " pokeagent evaluates it after every session and completes the run when it holds."
            for check in active
        ] or [
            "No authoritative success check: the run continues until the user stops it. You may propose a check"
            " (propose_success_check) or, when no check can show it, report the objective complete."
        ]
        if proposed:
            check = proposed[-1]
            lines.append(
                f"Your proposed check #{check['id']}: {objectives.describe(check['condition'])};"
                f" {now(check['condition'])}."
                " When it holds after a session, the run pauses for the user to confirm."
            )
        if in_game:
            values = ", ".join(f"{name}={value}" for name, value in observations.items() if name != "in_game")
            lines.append(f"Observations for checks: {values}.")
        return lines

    def loop(self, url: str, max_sessions: int | None = None) -> str:
        """Run sessions until something stops the run; return why."""
        progress_file = self.run_dir / "state.json"
        progress = json.loads(progress_file.read_text()) if progress_file.exists() else {"sessions": 0}
        failures = started = 0
        # A development request, run only once the objective is judged still open. A request left unhandled by a
        # previous invocation (stopped by --max-sessions or a crash) is recovered from its event.
        handled = {
            e["data"]["request"]
            for kind in ("development_decision", "development_cycle")
            for e in self.memory.events(kind, limit=-1)
        }
        open_requests = [
            e["id"] for e in self.memory.events("development_requested", limit=-1) if e["id"] not in handled
        ]
        pending = open_requests[-1] if open_requests else None
        while True:
            if verdict := objectives.judge(self.memory, self.game.observations()):
                self.log(verdict, self.memory.status()[1])
                self.show()
                return verdict
            if pending is not None and (max_sessions is None or started < max_sessions):
                started += self._develop(pending, progress)
            pending = None
            if max_sessions is not None and started >= max_sessions:
                return "max_sessions"
            started += 1
            progress["sessions"] += 1
            number = progress["sessions"]
            if self.game.registry.staged():  # validated tools become active at a session boundary, after a checkpoint
                self.checkpoint("before activating validated tools")
                for note in self.game.registry.activate_staged(number):
                    self.log("activate", note)
            session = self.server.open_session(number, self.budget)
            self.number = number
            spec = SessionSpec(
                brief=self.brief(number, progress),
                system_prompt_file=PROMPTS / "play.md",
                mcp_url=url,
                token=session.token,
                cwd=self.run_dir,
                stream_path=self.run_dir / "sessions" / f"{number:04d}.jsonl",
                web_log=self.run_dir / "web.jsonl",
                max_turns=self.budget + 20,
                timeout=self.session_timeout,
                model=self.model,
                effort=self.effort,
            )
            progress_file.write_text(json.dumps(progress, indent=1) + "\n")  # session numbers are never reused
            self.log("session", f"{number} start")
            self.memory.event("session_start", {}, number)
            self.show(f"session {number} (play)")
            try:
                result: SessionResult = self.provider(spec, self.on_event)
            finally:
                self.server.close_session(session)
            if session.summary:
                progress["summary"], progress["next_focus"] = session.summary, session.next_focus
            elif session.actions:
                last = "; ".join(e["data"]["outcome"] for e in self.memory.events("action", session=number, limit=5))
                progress["summary"] = (
                    f"(no summary: session {number} ended with {result.end} after {session.actions} actions;"
                    f" its last actions: {last})"
                )
            progress_file.write_text(json.dumps(progress, indent=1) + "\n")
            self.checkpoint(f"session {number}")
            self.log(
                "end",
                f"session {number}: {result.end}, {result.num_turns} turns, {session.actions} actions, "
                f"${result.cost_usd:.2f}" + (f", {result.error}" if result.error else ""),
            )
            ended = {"end": result.end, "turns": result.num_turns, "actions": session.actions}
            self.memory.event("session_end", ended | {"cost_usd": result.cost_usd, "usage": result.usage}, number)
            self.show()
            if session.claim:
                objectives.claim(self.memory, f"the agent reported the objective complete: {session.claim}")
                self.log("claimed", session.claim)
                self.show()
                return "claimed"
            if result.end == "auth_error":
                raise ProviderError(f"the Claude CLI lost its login: {result.error}")
            if result.end == "limit":
                failures = 0
                self._wait_for_reset(result)
            elif result.end == "error":
                failures += 1
                if failures >= MAX_FAILURES:
                    return "errors"
                self.sleep(30 * failures)
            else:
                failures = 0
            climbed, self._climbed = self._climbed, 0
            if climbed >= 4 and (verdict := objectives.judge(self.memory, self.game.observations())):
                self.log(verdict, self.memory.status()[1])  # success beats the stuck consequences
                self.show()
                return verdict
            if climbed == 4 and self._restore_progress():
                progress = json.loads(progress_file.read_text())
            elif climbed >= 4:
                self.log("stuck", f"the run pauses after {self.ladder.since} actions without progress")
                return "stuck"
            pending = session.request

    def _wait_for_reset(self, result: SessionResult) -> None:
        wait = max(60.0, result.resets_at - time.time()) if result.resets_at else 900.0
        self.log("limit", f"waiting {wait / 60:.0f} min for the usage limit to reset")
        start = time.monotonic()
        try:
            self.sleep(wait)
        finally:  # the time actually waited, also when Ctrl-C cuts it short; stats leave it out of elapsed time
            self.memory.event("limit_wait", {"seconds": time.monotonic() - start})

    def _develop(self, request_id: int, progress: dict) -> int:
        """Apply the policy to a development request; run the develop session when allowed. Returns the number of
        sessions run (0 or 1)."""
        found = next(e for e in self.memory.events("development_requested", limit=-1) if e["id"] == request_id)
        request = found["data"] | {"id": request_id}
        if reason := develop.denies(self.memory, request, self.dev_share, self.dev_attempts):
            decision = {"request": request_id, "tool": request["tool"], "reason": reason}
            self.memory.event("development_decision", decision)
            self.log("develop", f"request #{request_id} not run: {reason}")
            return 0
        cycles = self.memory.events("development_cycle", limit=-1)
        attempt = 1 + sum(1 for cycle in cycles if cycle["data"]["tool"] == request["tool"])
        progress["sessions"] += 1
        number = progress["sessions"]
        checkpoints = partial(restorable, self.run_dir, self.memory)
        bench = self.bench_class(self.run_dir, request, self.game, Game, checkpoints, self.dev_validations)
        session = self.server.open_session(number, develop.DEV_BUDGET, mode="develop")
        self.server.bench = bench
        try:
            spec = SessionSpec(
                brief=develop.brief(self.memory, request, bench, self.meta["objective"], attempt, self.dev_attempts),
                system_prompt_file=PROMPTS / "develop.md",
                mcp_url=self.server.dev_url,
                token=session.token,
                cwd=self.run_dir,
                stream_path=self.run_dir / "sessions" / f"{number:04d}.jsonl",
                web_log=self.run_dir / "web.jsonl",
                max_turns=develop.DEV_BUDGET + 100,
                timeout=self.dev_timeout,
                model=self.model,
                effort=self.effort,
            )
            (self.run_dir / "state.json").write_text(json.dumps(progress, indent=1) + "\n")
            self.log("session", f"{number} start (develop {request['tool']}, attempt {attempt})")
            self.memory.event("session_start", {"mode": "develop"}, number)
            self.show(f"session {number} (develop {request['tool']})")
            result: SessionResult = self.provider(spec, self.on_event)
        finally:
            self.server.close_session(session)
            self.server.bench = None
            bench.close()
        staged = next((r["version"] for r in reversed(bench.reports) if r["passed"]), None)
        cycle = {"request": request_id, "tool": request["tool"], "attempt": attempt, "summary": session.summary}
        self.memory.event("development_cycle", cycle | {"validations": len(bench.reports), "staged": staged}, number)
        ended = {"end": result.end, "turns": result.num_turns, "actions": session.actions, "mode": "develop"}
        self.memory.event("session_end", ended | {"cost_usd": result.cost_usd, "usage": result.usage}, number)
        self.show()
        self.log(
            "end",
            f"session {number} (develop): {result.end}, {result.num_turns} turns, {len(bench.reports)} validations,"
            f" staged {staged or 'nothing'}, ${result.cost_usd:.2f}",
        )
        if result.end == "auth_error":
            raise ProviderError(f"the Claude CLI lost its login: {result.error}")
        if result.end == "limit":
            self._wait_for_reset(result)
        return 1


def latest_path(run_dir: Path) -> Path | None:
    """The file of the game as it was after the last action (or restore), else the latest checkpoint, else None."""
    if (recovery := run_dir / "recovery.state").exists():
        return recovery
    return saved[-1] if (saved := sorted((run_dir / "checkpoints").glob("*.state"))) else None


def latest_state(run_dir: Path) -> bytes | None:
    """The latest game state's bytes, or None (power-on)."""
    path = latest_path(run_dir)
    return path.read_bytes() if path else None


def restorable(run_dir: Path, memory: Memory) -> list[dict]:
    """The checkpoint events of the current timeline's history whose files still exist, oldest first."""
    history = memory.history()
    return [
        event
        for event in memory.events("checkpoint", limit=-1)
        if event["created"] <= history.get(event["timeline"], -1)
        and (run_dir / "checkpoints" / event["data"]["file"]).exists()
    ]


def restore(run_dir: Path, memory: Memory, name: str) -> dict:
    """Take the run back to checkpoint `name` on a new timeline (see Memory.branch). The game, notes and planned focus
    become the checkpoint's; the session count stays. The next brief says what changed. Call under `run_lock`."""
    meta = json.loads((run_dir / "run.json").read_text())
    target = next((e for e in reversed(restorable(run_dir, memory)) if e["data"]["file"] == name), None)
    if target is None:
        raise ValueError(f"{name} is not a saved checkpoint of this run's current timeline or its history")
    if any(target["data"].get(key) != meta[key] for key in IDENTITY):
        raise ValueError(f"{name} was saved with another ROM, core or core options than this run's")
    path = run_dir / "checkpoints" / name
    notes, progress_file = run_dir / "notes.md", run_dir / "state.json"
    old_notes = notes.read_text() if notes.exists() else ""
    progress = json.loads(progress_file.read_text()) if progress_file.exists() else {"sessions": 0}
    history = memory.history()
    undone = sum(
        1
        for e in memory.events("action", after=target["id"], limit=-1)
        if e["created"] <= history.get(e["timeline"], -1)
    )
    reverted = [
        e["data"]["text"]
        for e in memory.events("milestone", after=target["id"], limit=-1)
        if e["created"] <= history.get(e["timeline"], -1)
    ]
    # Read and write every file before the timeline changes, so a failure (disk full) leaves the run as it was.
    saved_notes = path.with_suffix(".notes.md")
    new_notes = saved_notes.read_text() if saved_notes.exists() else ""
    tkv = run_dir / "tkv.json"  # the tools' timeline store goes back with the game
    saved_tkv = path.with_suffix(".tkv.json")
    old_tkv, new_tkv = (f.read_bytes() if f.exists() else None for f in (tkv, saved_tkv))
    saved_progress = path.with_suffix(".state.json")
    restored = json.loads(saved_progress.read_text()) if saved_progress.exists() else {}
    restored["sessions"] = progress["sessions"]
    old_state = (run_dir / "recovery.state").read_bytes() if (run_dir / "recovery.state").exists() else None
    try:
        write_atomic(run_dir / "recovery.state", path.read_bytes())
        _put(run_dir / "recovery.tkv.json", new_tkv)
        write_atomic(notes, new_notes.encode())
        write_atomic(progress_file, (json.dumps(restored, indent=1) + "\n").encode())
        _put(tkv, new_tkv)
        stale = memory.branch(target)
    except BaseException:
        if old_state is not None:
            write_atomic(run_dir / "recovery.state", old_state)
        _put(run_dir / "recovery.tkv.json", old_tkv)
        write_atomic(notes, old_notes.encode())
        write_atomic(progress_file, (json.dumps(progress, indent=1) + "\n").encode())
        _put(tkv, old_tkv)
        raise
    kept = set(new_notes.splitlines())
    data = {
        "file": name,
        "reason": target["data"]["reason"],
        "saved": time.strftime("%Y-%m-%d %H:%M", time.localtime(target["created"])),
        "undone_actions": undone,
        "reverted_milestones": reverted,
        "stale": stale,
        "dropped_notes": [line for line in old_notes.splitlines() if line.strip() and line not in kept][:40],
    }
    if memory.status()[0] != "running":  # completion after the checkpoint is undone with the rest of the timeline
        memory.set_status("running")
    memory.event("restore", data)
    return data


def _put(path: Path, data: bytes | None) -> None:
    """Write `data` atomically, or remove the file when there is none."""
    if data is None:
        path.unlink(missing_ok=True)
    else:
        write_atomic(path, data)


def restore_notice(data: dict) -> str:
    lines = [
        f"The game was restored to the checkpoint saved {data['saved']} ({data['reason']}): {data['undone_actions']}"
        " later actions were undone, so the game is back where it was then. Your notes and planned focus are the"
        " ones saved with it.",
    ]
    if data.get("reverted_milestones"):
        lines.append(f"Milestones reached after it are undone: {'; '.join(data['reverted_milestones'])}.")
    if data["stale"]:
        lines.append(f"Progress knowledge recorded after it is now stale: {', '.join(f'#{i}' for i in data['stale'])}.")
    if data["dropped_notes"]:
        lines.append("Note lines from the undone part (what you learned there may still hold):")
        lines += [f"  {line}" for line in data["dropped_notes"]]
    return "\n".join(lines)


def _run_rom(run_dir: Path) -> tuple[dict, Path, Gen1]:
    """The run's metadata, ROM path and adapter, once the ROM is checked to be the one the run started with."""
    meta = json.loads((run_dir / "run.json").read_text())
    rom = Path(meta["rom"])
    data = rom.read_bytes()
    if hashlib.sha1(data).hexdigest() != meta["rom_sha1"]:
        raise ValueError(f"{rom} is no longer the ROM this run started with")
    return meta, rom, Gen1(data)


def _states(run_dir: Path, memory: Memory, checkpoint: str | None) -> list[tuple[str, bytes]]:
    """(label, state) to show or save, newest first: one checkpoint, or the latest state and then the checkpoints of
    the current timeline's history."""
    if checkpoint:
        if not any(e["data"]["file"] == checkpoint for e in restorable(run_dir, memory)):
            raise ValueError(f"{checkpoint} is not a checkpoint of this run's current timeline; see pokeagent restore")
        return [(f"checkpoint {checkpoint}", (run_dir / "checkpoints" / checkpoint).read_bytes())]
    found = [("the latest state", state)] if (state := latest_state(run_dir)) else []
    for event in reversed(restorable(run_dir, memory)):
        saved = time.strftime("%Y-%m-%d %H:%M", time.localtime(event["created"]))
        label = f"checkpoint {event['data']['file']} ({event['data']['reason']}, {saved})"
        found.append((label, (run_dir / "checkpoints" / event["data"]["file"]).read_bytes()))
    return found


def _viewer(watch: bool, web: tuple[str, int] | None):
    """(what the game and runner show things on, the web server or None): the Tk window when `watch`, the web page at
    `web` (host, port); both show the same object. (None, None) for neither."""
    if not (watch or web):
        return None, None
    from types import SimpleNamespace

    from pokeagent import viewer

    shown = viewer.Viewer() if watch else SimpleNamespace(frame=None, status="", panel="")
    return shown, viewer.serve(shown, web) if web else None


def view(
    run_dir: Path, data_dir: Path, checkpoint: str | None = None, watch: bool = True, web: tuple[str, int] | None = None
) -> None:
    """Show a run's game, at its latest state or a checkpoint, with the status panel, in the window and/or on the web
    page. Nothing is played or changed; returns when the window is closed or on Ctrl-C."""
    if not (watch or web):
        raise ValueError("nothing to show it in: drop --no-watch or pass --web")
    meta, rom, _ = _run_rom(run_dir)
    with contextlib.closing(open_memory(run_dir, data_dir)) as memory:
        found = _states(run_dir, memory, checkpoint)[:1]
        panel = stats.status(memory, run_dir)
    with Emulator(core_path(meta["core"]), rom, options=meta["core_options"]) as emu:
        if found:
            emu.load_state(found[0][1])
        emu.run([], 1)  # draw the loaded state
        frame = emu.frame()
    viewer, page = _viewer(watch, web)
    viewer.frame, viewer.panel = frame, panel
    quit = "close the window or press Ctrl-C" if watch else "press Ctrl-C in the terminal"
    viewer.status = f"{run_dir.name}: {found[0][0] if found else 'power-on'} (viewing only; {quit} to quit)"
    closed = viewer.closed if watch else threading.Event()
    try:
        with contextlib.suppress(KeyboardInterrupt):
            while not closed.wait(0.2):  # a short wait keeps Ctrl-C working
                pass
    finally:
        if page:
            page.shutdown()


def play_by_hand(
    run_dir: Path,
    data_dir: Path,
    checkpoint: str | None = None,
    out: Path | None = None,
    force: bool = False,
    web: tuple[str, int] | None = None,
) -> str:
    """Play a copy of the run's game yourself with the keyboard (`viewer.KEYS`), from its latest state or a checkpoint,
    until the window closes or Ctrl-C. The run does not change. With `out`, an in-game save you made is written there
    as a .sav; returns what was kept. No sound: the emulator worker discards audio."""
    if out and out.exists() and not force:
        raise ValueError(f"{out} exists; pass --force to replace it or choose another --out")
    from pokeagent.viewer import KEY_HELP

    meta, rom, adapter = _run_rom(run_dir)
    with contextlib.closing(open_memory(run_dir, data_dir)) as memory:
        label, state = (_states(run_dir, memory, checkpoint) or [("power-on", None)])[0]
    viewer, page = _viewer(True, web)
    keep = f"save in game to keep it in {out.name}" if out else "nothing is kept; --out keeps your in-game save"
    viewer.status = f"{run_dir.name}: {label}, a copy ({keep}). Close the window to stop."
    viewer.panel = KEY_HELP
    try:
        with Emulator(core_path(meta["core"]), rom, options=meta["core_options"]) as emu:
            if state:
                emu.load_state(state)
            before, tick, due = emu.save_ram(), 2 / emu.info["fps"], time.monotonic()
            # Ctrl-C only sets a flag, so it never lands between an emulator request and its reply.
            stop = threading.Event()
            previous = signal.signal(signal.SIGINT, lambda *_: stop.set())
            try:
                while not (viewer.closed.is_set() or stop.is_set()):
                    emu.run(sorted(viewer.buttons), 2)  # two frames per step: the window redraws at 30 fps
                    viewer.frame = emu.frame()
                    due = max(due + tick, time.monotonic() - tick)  # after a stall, carry on rather than rush
                    time.sleep(max(0.0, due - time.monotonic()))
            finally:
                signal.signal(signal.SIGINT, previous)
            sram = emu.save_ram()
    finally:
        if page:
            page.shutdown()
    if out is None:
        return "nothing kept"
    if sram == before or not adapter.save_complete(sram):
        return "you did not save in game; nothing written"
    write_atomic(out, sram)
    return f"wrote your in-game save to {out}"


def _where(run_dir: Path) -> str:
    """Where a run's game stands, read from its latest state on a fresh emulator."""
    if (saved := latest_state(run_dir)) is None:
        return "not started"
    try:
        meta, rom, adapter = _run_rom(run_dir)
        with Emulator(core_path(meta["core"]), rom, options=meta["core_options"]) as emu:
            emu.load_state(saved)
            state = adapter.state(emu)
    except (OSError, ValueError, EmulatorError) as e:  # a moved or changed ROM should not hide the other runs
        return f"cannot read its game: {e}"
    if not state["in_game"]:
        return "before the game starts (title screen or power-on)"
    party = state["party"]
    team = f"{len(party)} Pokémon up to level {max(p['level'] for p in party)}" if party else "no Pokémon yet"
    return f"{state['map']}, {state['badges']} badges, {team}"


def run_rows(data_dir: Path, where=_where) -> list[dict]:
    """One dict per run in `data_dir`, oldest first: id, state (with its reason), play sessions, actions, and where
    its game stands (`where(run_dir)`)."""
    rows = []
    for meta in sorted((data_dir / "runs").glob("*/run.json")):
        run_dir = meta.parent
        with contextlib.closing(open_memory(run_dir, data_dir)) as memory:
            state, reason = memory.status()
            played = [r for r in stats.sessions(memory) if r["mode"] == "play"]
        if state == "running" and not stats.live(run_dir):  # the store says running until the run ends or pauses
            state, reason = "stopped", ""
        rows.append(
            {
                "id": run_dir.name,
                "state": f"{state} ({reason})" if reason else state,
                "sessions": len(played),
                "actions": sum(r["actions"] for r in played),
                "where": where(run_dir),
            }
        )
    return rows


def list_runs(data_dir: Path) -> str:
    """One line per run in `data_dir`, oldest first: its id, state, play sessions and actions, and where its game
    stands."""
    lines = [
        f"{r['id']}  {r['state']}; {r['sessions']} sessions, {r['actions']:,} actions; {r['where']}"
        for r in run_rows(data_dir)
    ]
    return "\n".join(lines) or f"no runs in {data_dir / 'runs'}"


def export_save(run_dir: Path, data_dir: Path, out: Path, checkpoint: str | None = None, force: bool = False) -> str:
    """Write the run's game as an in-game save to `out`: the cartridge's battery RAM, the standard .sav other
    emulators load. The game is saved through its START menu on a copy of the state, so the run does not change. The
    newest state that can be saved is used (not one in a battle or at a yes/no prompt); returns which one."""
    if out.exists() and not force:
        raise ValueError(f"{out} exists; pass --force to replace it or choose another --out")
    meta, rom, adapter = _run_rom(run_dir)
    with contextlib.closing(open_memory(run_dir, data_dir)) as memory:
        found = _states(run_dir, memory, checkpoint)
    with Emulator(core_path(meta["core"]), rom, options=meta["core_options"]) as emu:
        for label, state in found:
            emu.load_state(state)
            if adapter.save_game(emu) and adapter.save_complete(sram := emu.save_ram()):
                write_atomic(out, sram)
                return label
    raise ValueError(
        "no state of this run could be saved in game (each was in a battle or at a yes/no prompt)"
        if found
        else "this run has no game state yet"
    )


def play(
    run_dir: Path,
    data_dir: Path,
    *,
    max_sessions: int | None = None,
    model: str | None = None,
    effort: str | None = None,
    watch: bool = False,
    web: tuple[str, int] | None = None,
    speed: float | None = None,
    development: dict | None = None,
) -> str:
    """Play a run from where it stopped (or power-on) until something stops it; return why. `development` holds the
    Runner's dev_* policy settings."""
    run_dir = run_dir.resolve()
    meta, rom, adapter = _run_rom(run_dir)
    with run_lock(run_dir), contextlib.closing(open_memory(run_dir, data_dir)) as memory:
        status, why = memory.status()
        if status != "running":
            settle = f"; settle it with `pokeagent objective confirm|reject {run_dir.name}`"
            raise ValueError(f"run {run_dir.name} is {status} ({why})" + (settle if status == "claimed" else ""))
        version = preflight()
        viewer, page = _viewer(watch, web)
        start = partial(Emulator, core_path(meta["core"]), rom, options=meta["core_options"])
        game = Game(
            start(), adapter, speed, viewer, start, Registry(memory), data_dir / "library", run_dir / "tkv.json"
        )
        try:
            if state := latest_state(run_dir):
                game.load_state(state)
            if viewer:
                viewer.frame = game.emu.frame()
            server = GameServer(game, memory, run_dir / "notes.md", data_dir / "harness-issues.md")
            runner = Runner(run_dir, game, server, model=model, effort=effort, **(development or {}))
            runner.log("start", f"{meta['game']}; {version}; from {'the last state' if state else 'power-on'}")
            url = server.start()
            reason = "error"
            try:
                reason = runner.loop(url, max_sessions)
            except KeyboardInterrupt:
                reason = "interrupted"
            except EmulatorError as e:
                reason = f"the emulator failed ({e}); resume continues from the last state"
            finally:
                with contextlib.suppress(EmulatorError):  # nothing to save once the emulator is gone
                    runner.checkpoint("stop")
                server.stop()
                runner.log("stop", reason)
            return reason
        finally:
            game.emu.close()
            if page:
                page.shutdown()
