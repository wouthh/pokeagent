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
import shutil
import threading
import time
from functools import partial
from pathlib import Path

from PIL import Image

from pokeagent import objectives
from pokeagent.agents.claude_cli import ProviderError, SessionResult, SessionSpec, preflight, run_session
from pokeagent.emulator.client import Emulator, EmulatorError
from pokeagent.emulator.cores import core_path
from pokeagent.games.pokemon_gen1 import (
    CORE,
    CORE_OPTIONS,
    DEFAULT_CHECK,
    DEFAULT_OBJECTIVE,
    FAMILY,
    OBSERVATIONS,
    Gen1,
)
from pokeagent.memory import Memory, describe_knowledge
from pokeagent.server import GameServer

PROMPTS = Path(__file__).with_name("prompts")
BUTTONS = ("a", "b", "start", "select", "up", "down", "left", "right")
DIRECTIONS = ("up", "down", "left", "right")
SETTLE_FRAMES = 20  # map events (Oak in Pallet Town) start a few frames after a walk ends
KEEP_CHECKPOINTS = 10
CONTEXT_FILES = ("notes.md", "state.json")
CHECKPOINT_EVERY = 25  # actions
MAX_FAILURES = 5
MAX_RESTARTS = 3  # emulator restarts per play
KNOWLEDGE_HITS = 5
IDENTITY = ("rom_sha1", "core", "core_options")  # what a checkpoint must share with its run to be restored
LEGEND = "@ you, S person or object, W warp/door, . walkable, , grass, v < > ^ ledges, # blocked"


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


class Game:
    """The live emulator and adapter. One lock serialises every call. The adapter drives this object as its
    emulator, so `run` can pace emulation (`speed` x real time) and feed the viewer. `start` makes a new emulator
    for `restart`."""

    def __init__(self, emu: Emulator, adapter: Gen1, speed: float | None = None, viewer=None, start=None):
        self.emu, self.adapter, self.speed, self.viewer, self.start = emu, adapter, speed, viewer, start
        self.lock = threading.Lock()
        self.fps = emu.info["fps"]
        self._shown = 0.0

    def read(self, addr: int, n: int) -> bytes:
        return self.emu.read(addr, n)

    def run(self, buttons=(), frames: int = 1) -> None:
        self.emu.run(buttons, frames)
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

    def state_text(self) -> str:
        with self.lock:
            return self.adapter.state_text(self.adapter.state(self))

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
            text = self.adapter.state_text(state)
            if state["in_game"]:
                grid = self.adapter.grid(self)
                text += f"\n\nWhole map ({LEGEND}):\n{grid.render((state['x'], state['y']))}"
            return text, self.png()

    def act(self, name: str, **args) -> tuple[str, bytes | None]:
        """Run one macro action; return a compact report and a screenshot when the screen mode changed."""
        with self.lock:
            before = self.adapter.state(self)
            outcome = self._do(name, args)
            after = self.adapter.state(self)
            old, new = self.adapter.fingerprint(before), self.adapter.fingerprint(after)
            changed = [group for group in new if new[group] != old[group]]
            lines = [f"{name}: {outcome}", f"Changed: {', '.join(changed) or 'nothing'}"]
            if not after["in_game"] or set(changed) & {"party", "bag", "money", "battle"}:
                lines.append(self.adapter.state_text(after))
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
        adapter = self.adapter
        if name in ("walk", "walk_to") and not adapter.state(self)["in_game"]:
            raise ValueError("no game is in progress yet; use press and advance_text until one is")
        match name:
            case "press":
                button, times = args["button"], args["times"]
                if button not in BUTTONS:
                    raise ValueError(f"button must be one of {', '.join(BUTTONS)}")
                if not 1 <= times <= 10:
                    raise ValueError("times must be 1..10")
                for _ in range(times):
                    adapter.press(self, button)
                return f"pressed {button} x{times}"
            case "walk":
                direction, steps = args["direction"], args["steps"]
                if direction not in DIRECTIONS:
                    raise ValueError(f"direction must be one of {', '.join(DIRECTIONS)}")
                if not 1 <= steps <= 20:
                    raise ValueError("steps must be 1..20")
                result = adapter.walk(self, direction, steps)
            case "walk_to":
                result = adapter.walk_to(self, args["x"], args["y"])
            case "advance_text":
                result = adapter.advance_text(self)
                return f"{result['outcome']} after {result['presses']} A press(es)"
            case _:
                raise ValueError(f"unknown action {name}")
        self.run([], SETTLE_FRAMES)
        return f"{result['outcome']} after {result['steps']} step(s)"


def new_run(rom: Path, objective: str | None, data_dir: Path, success: dict | None = None) -> Path:
    """Create a run directory for a supported ROM. Only the adapter's default objective gets its trusted check;
    `success` is the user's own check."""
    data = rom.read_bytes()
    adapter = Gen1(data)  # refuses unsupported ROMs
    if success is not None:
        objectives.validate(success, [name for name in OBSERVATIONS if name != "in_game"])
    (data_dir / "runs").mkdir(parents=True, exist_ok=True, mode=0o700)
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
    }
    (run_dir / "run.json").write_text(json.dumps(meta, indent=1) + "\n")
    return run_dir


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
    ):
        run_dir = run_dir.resolve()  # the Claude child runs with cwd=run_dir, so session paths must be absolute
        self.run_dir, self.game, self.server, self.provider, self.sleep = run_dir, game, server, provider, sleep
        self.budget, self.session_timeout, self.model, self.effort = budget, session_timeout, model, effort
        self.meta = json.loads((run_dir / "run.json").read_text())
        self.memory: Memory = server.memory
        self._actions = self.restarts = 0
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
        self.memory.event("checkpoint", {"file": path.name, "reason": reason} | identity)
        self.log("checkpoint", f"{path.name} ({reason})")

    def _after_action(self) -> None:
        state = self.game.save_state()  # one emulator save: a second one could fail after the action is committed
        write_atomic(self.run_dir / "recovery.state", state)
        self._actions += 1
        if self._actions % CHECKPOINT_EVERY == 0:
            self.checkpoint(f"{self._actions} actions", state)

    def recover(self, error: str) -> bool:
        """Restart a failed emulator at the state after the last completed action; False once restarts run out."""
        if self.restarts >= MAX_RESTARTS:
            return False
        self.restarts += 1
        self.game.restart(latest_state(self.run_dir))
        self.memory.event("restart", {"error": error})
        self.log("restart", f"the emulator was restarted after: {error}")
        return True

    def brief(self, number: int, progress: dict) -> str:
        notes = self.run_dir / "notes.md"
        observations = self.game.observations()
        lines = [f"Objective: {self.meta['objective']}", *self._checks(observations), ""]
        last_start = (self.memory.events("session_start", limit=1) or [{"id": 0}])[0]["id"]
        for restored in self.memory.events("restore", after=last_start):
            lines += [restore_notice(restored["data"]), ""]
        for rejected in self.memory.events("claim_rejected", after=last_start):
            data = rejected["data"]
            lines += [
                f"The user rejected the completion claim ({data['claim']}): {data['why'] or 'no reason given'}",
                "",
            ]
        lines += [
            f"This is session {number}. You have {self.budget} game actions (press, walk, walk_to, advance_text);"
            " get_state, notes and knowledge tools are free.",
            f"Last session's summary: {progress.get('summary') or 'none, this is the first session.'}",
            f"Planned focus: {progress.get('next_focus') or 'none'}",
            "",
            "Your notes:",
            (notes.read_text() if notes.exists() else "") or "(empty)",
        ]
        if hits := self.memory.search(f"{self.game.location()} {progress.get('next_focus') or ''}", KNOWLEDGE_HITS):
            lines += ["", "Knowledge that may bear on this (knowledge_search finds more):"]
            lines += [describe_knowledge(entry) for entry in hits]
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
        while True:
            if verdict := objectives.judge(self.memory, self.game.observations()):
                self.log(verdict, self.memory.status()[1])
                return verdict
            if max_sessions is not None and started >= max_sessions:
                return "max_sessions"
            started += 1
            progress["sessions"] += 1
            number = progress["sessions"]
            session = self.server.open_session(number, self.budget)
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
            if session.claim:
                objectives.claim(self.memory, f"the agent reported the objective complete: {session.claim}")
                self.log("claimed", session.claim)
                return "claimed"
            if result.end == "auth_error":
                raise ProviderError(f"the Claude CLI lost its login: {result.error}")
            if result.end == "limit":
                failures = 0
                wait = max(60.0, result.resets_at - time.time()) if result.resets_at else 900.0
                self.log("limit", f"waiting {wait / 60:.0f} min for the usage limit to reset")
                self.sleep(wait)
            elif result.end == "error":
                failures += 1
                if failures >= MAX_FAILURES:
                    return "errors"
                self.sleep(30 * failures)
            else:
                failures = 0


def latest_state(run_dir: Path) -> bytes | None:
    """The game as it was after the last action (or restore), else the latest checkpoint, else None (power-on)."""
    if (recovery := run_dir / "recovery.state").exists():
        return recovery.read_bytes()
    if saved := sorted((run_dir / "checkpoints").glob("*.state")):
        return saved[-1].read_bytes()
    return None


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
    stale = memory.branch(target)
    write_atomic(run_dir / "recovery.state", path.read_bytes())
    saved_notes = path.with_suffix(".notes.md")
    new_notes = saved_notes.read_text() if saved_notes.exists() else ""
    write_atomic(notes, new_notes.encode())
    saved_progress = path.with_suffix(".state.json")
    restored = json.loads(saved_progress.read_text()) if saved_progress.exists() else {}
    restored["sessions"] = progress["sessions"]
    progress_file.write_text(json.dumps(restored, indent=1) + "\n")
    kept = set(new_notes.splitlines())
    data = {
        "file": name,
        "reason": target["data"]["reason"],
        "saved": time.strftime("%Y-%m-%d %H:%M", time.localtime(target["created"])),
        "undone_actions": undone,
        "stale": stale,
        "dropped_notes": [line for line in old_notes.splitlines() if line.strip() and line not in kept][:40],
    }
    if memory.status()[0] != "running":  # completion after the checkpoint is undone with the rest of the timeline
        memory.set_status("running")
    memory.event("restore", data)
    return data


def restore_notice(data: dict) -> str:
    lines = [
        f"The game was restored to the checkpoint saved {data['saved']} ({data['reason']}): {data['undone_actions']}"
        " later actions were undone, so the game is back where it was then. Your notes and planned focus are the"
        " ones saved with it.",
    ]
    if data["stale"]:
        lines.append(f"Progress knowledge recorded after it is now stale: {', '.join(f'#{i}' for i in data['stale'])}.")
    if data["dropped_notes"]:
        lines.append("Note lines from the undone part (what you learned there may still hold):")
        lines += [f"  {line}" for line in data["dropped_notes"]]
    return "\n".join(lines)


def play(
    run_dir: Path,
    data_dir: Path,
    *,
    max_sessions: int | None = None,
    model: str | None = None,
    effort: str | None = None,
    watch: bool = False,
    speed: float | None = None,
) -> str:
    """Play a run from where it stopped (or power-on) until something stops it; return why."""
    run_dir = run_dir.resolve()
    meta = json.loads((run_dir / "run.json").read_text())
    rom = Path(meta["rom"])
    data = rom.read_bytes()
    if hashlib.sha1(data).hexdigest() != meta["rom_sha1"]:
        raise ValueError(f"{rom} is no longer the ROM this run started with")
    adapter = Gen1(data)
    with run_lock(run_dir), contextlib.closing(open_memory(run_dir, data_dir)) as memory:
        status, why = memory.status()
        if status != "running":
            settle = f"; settle it with `pokeagent objective confirm|reject {run_dir.name}`"
            raise ValueError(f"run {run_dir.name} is {status} ({why})" + (settle if status == "claimed" else ""))
        version = preflight()
        viewer = None
        if watch:
            from pokeagent.viewer import Viewer

            viewer = Viewer()
        start = partial(Emulator, core_path(meta["core"]), rom, options=meta["core_options"])
        game = Game(start(), adapter, speed, viewer, start)
        try:
            if state := latest_state(run_dir):
                game.load_state(state)
            if viewer:
                viewer.frame = game.emu.frame()
            server = GameServer(game, memory, run_dir / "notes.md", data_dir / "harness-issues.md")
            runner = Runner(run_dir, game, server, model=model, effort=effort)
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
