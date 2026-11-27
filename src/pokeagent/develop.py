"""Develop mode: a session that builds or improves one code tool, away from the live game.

A play session asks with `request_development`; after it ends the runner applies the policy and, when allowed, runs a
develop session. That session has its own tool list (its own MCP endpoint): a development emulator started from a
copy of the live state (or an earlier checkpoint of the current timeline), read-only memory tools, fixtures, a
workspace per tool, the active library read-only, and `validate`. Its controller tools never reach the live game,
and its actions are not game events. A tool that validates is staged and becomes active at the next play session.
"""

import json
import re
from pathlib import Path

from pokeagent import capabilities, validation

NAME = re.compile(r"[a-z][a-z0-9_]{1,39}")
WS_FILES, WS_BYTES = 200, 5 << 20  # workspace files (fixtures excluded)
MAX_FIXTURES, FIXTURE_BYTES = 20, 1 << 20
MAX_SNAPSHOTS = 16
WRAM = (0xC000, 0x2000)  # work RAM: the snapshot, diff and search range
DEV_BUDGET = 200  # controller actions per develop session
TOOL_FILES = ("manifest.json", "tool.py", "test_tool.py")


class Bench:
    """One develop session's world. `live` is the run's Game: the bench copies its state and shares its adapter,
    registry and library, but drives its own emulator."""

    def __init__(self, run_dir: Path, request: dict, live, game_class, checkpoints, max_validations: int = 10):
        self.run_dir, self.request, self.live, self.checkpoints = run_dir, request, live, checkpoints
        self.folder = run_dir / "workspace" / request["tool"]  # a second attempt continues the same workspace
        (self.folder / "fixtures").mkdir(parents=True, exist_ok=True, mode=0o700)
        self.start_state = live.save_state()
        self.game = game_class(
            live.start(),
            live.adapter,
            start=live.start,
            registry=live.registry,
            library=live.library,
            tkv=self.folder / "tkv.json",
        )
        self.game.load_state(self.start_state)
        self.game_class, self.snapshots, self.reports, self.max_validations = game_class, {}, [], max_validations

    def close(self) -> None:
        self.game.emu.close()

    # ---- the development game ----

    def load(self, checkpoint: str = "") -> str:
        """Restart the development game at the live state of this session's start, or at an earlier checkpoint."""
        if checkpoint:
            found = next((e for e in self.checkpoints() if e["data"]["file"] == checkpoint), None)
            if found is None:
                raise ValueError(f"{checkpoint} is not a checkpoint of the current timeline; see checkpoints")
            state = (self.run_dir / "checkpoints" / checkpoint).read_bytes()
        else:
            state = self.start_state
        self.game.restart(state)
        return f"loaded {checkpoint or 'the live state'}"

    def fixture_save(self, name: str) -> str:
        _check_name(name)
        path = self.folder / "fixtures" / f"{name}.state"
        if not path.exists() and len(self.fixtures()) >= MAX_FIXTURES:
            raise ValueError(f"at most {MAX_FIXTURES} fixtures per tool")
        state = self.game.save_state()
        if len(state) > FIXTURE_BYTES:
            raise ValueError("this state is too large for a fixture")
        path.write_bytes(state)
        return f"saved fixture {name}"

    def fixture_load(self, name: str) -> str:
        _check_name(name)
        path = self.folder / "fixtures" / f"{name}.state"
        if not path.exists():
            raise ValueError(f"no fixture {name}; saved: {', '.join(sorted(self.fixtures())) or 'none'}")
        self.game.load_state(path.read_bytes())
        return f"loaded fixture {name}"

    def fixtures(self) -> dict[str, bytes]:
        return {p.stem: p.read_bytes() for p in sorted((self.folder / "fixtures").glob("*.state"))}

    # ---- memory, read-only ----

    def snapshot(self, name: str) -> str:
        _check_name(name)
        if name not in self.snapshots and len(self.snapshots) >= MAX_SNAPSHOTS:
            raise ValueError(f"at most {MAX_SNAPSHOTS} snapshots per session")
        self.snapshots[name] = self.game.peek(*WRAM)
        return f"saved work RAM ({WRAM[0]:#06x}-{WRAM[0] + WRAM[1] - 1:#06x}) as {name}"

    def diff(self, before: str, after: str = "now") -> str:
        old, new = self._snapshot(before), self._snapshot(after)
        changed = [i for i in range(WRAM[1]) if old[i] != new[i]]
        lines = [f"{WRAM[0] + i:#06x}: {old[i]:#04x} -> {new[i]:#04x}" for i in changed[:200]]
        more = f"\n(and {len(changed) - 200} more)" if len(changed) > 200 else ""
        return f"{len(changed)} byte(s) differ\n" + "\n".join(lines) + more

    def search(self, value: int, width: int = 1, among: list[int] | None = None) -> str:
        """Work RAM addresses holding `value` now (1 byte, or 2 bytes in either order); `among` narrows an earlier
        result."""
        if width not in (1, 2) or not isinstance(value, int) or not 0 <= value < 256**width:
            raise ValueError("value must fit in `width` (1 or 2) bytes")
        data = self._snapshot("now")
        hits = []
        for i in range(WRAM[1] - width + 1):
            raw = data[i : i + width]
            if value in (int.from_bytes(raw, "little"), int.from_bytes(raw, "big")):
                hits.append(WRAM[0] + i)
        if among:
            hits = [a for a in hits if a in set(among)]
        shown = ", ".join(f"{a:#06x}" for a in hits[:100])
        return f"{len(hits)} address(es)" + (f": {shown}" if hits else "") + (" …" if len(hits) > 100 else "")

    def _snapshot(self, name: str) -> bytes:
        if name == "now":
            return self.game.peek(*WRAM)
        if name not in self.snapshots:
            raise ValueError(f"no snapshot {name}; taken: {', '.join(self.snapshots) or 'none'}")
        return self.snapshots[name]

    # ---- workspace ----

    def _path(self, path: str) -> Path:
        if not isinstance(path, str) or not path or path.startswith("/") or "\\" in path or "\0" in path:
            raise ValueError("paths are relative, like tool.py or notes/plan.md")
        parts = Path(path).parts
        if any(part in ("..", ".") for part in parts) or parts[0] == "fixtures":
            raise ValueError("paths stay inside the workspace, and fixtures/ is managed by fixture_save")
        target = self.folder.joinpath(*parts)
        for step in [target, *target.parents]:
            if step == self.folder:
                break
            if step.is_symlink():
                raise ValueError("the workspace holds no links")
        return target

    def files(self) -> list[Path]:
        return sorted(
            p for p in self.folder.rglob("*") if p.is_file() and not p.is_relative_to(self.folder / "fixtures")
        )

    def ws_list(self) -> str:
        listed = [f"{p.relative_to(self.folder)} ({p.stat().st_size} bytes)" for p in self.files()]
        fixtures = ", ".join(self.fixtures()) or "none"
        return "\n".join([*listed, f"fixtures: {fixtures}"]) if listed else f"(empty)\nfixtures: {fixtures}"

    def ws_read(self, path: str) -> str:
        target = self._path(path)
        if not target.is_file():
            raise ValueError(f"no file {path}")
        return target.read_text(errors="replace")

    def ws_write(self, path: str, content: str) -> str:
        target = self._path(path)
        others = [p for p in self.files() if p != target]
        if len(others) + 1 > WS_FILES:
            raise ValueError(f"the workspace holds at most {WS_FILES} files")
        if sum(p.stat().st_size for p in others) + len(content.encode()) > WS_BYTES:
            raise ValueError(f"the workspace holds at most {WS_BYTES >> 20} MB")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return f"wrote {path} ({len(content.encode())} bytes)"

    def ws_delete(self, path: str) -> str:
        target = self._path(path)
        if not target.is_file():
            raise ValueError(f"no file {path}")
        target.unlink()
        return f"deleted {path}"

    # ---- library and validation ----

    def library_read(self, name: str) -> str:
        tool = self.live.registry.get(name, "tool")
        spec = {k: v for k, v in tool["spec"].items() if k != "sha256"}
        source = (self.live.library / tool["spec"]["sha256"] / "tool.py").read_text()
        return f"{name} version {tool['version']}\nmanifest.json:\n{json.dumps(spec, indent=1)}\n\ntool.py:\n{source}"

    def validate(self, scope: str, session: int | None, observations: set[str], taken: set[str]) -> dict:
        if len(self.reports) >= self.max_validations:
            raise ValueError(f"this session's {self.max_validations} validations are used up")
        missing = [name for name in TOOL_FILES if not (self.folder / name).is_file()]
        if missing:
            raise ValueError(f"the workspace needs {', '.join(missing)} first")
        try:
            manifest = json.loads((self.folder / "manifest.json").read_text())
        except ValueError as e:
            raise ValueError(f"manifest.json is not JSON: {e}") from None
        if not isinstance(manifest, dict) or manifest.get("name") != self.request["tool"]:
            raise ValueError(f'manifest.json must be an object with "name": "{self.request["tool"]}"')
        files = {name: (self.folder / name).read_text() for name in ("tool.py", "test_tool.py")}
        fixtures = self.fixtures()
        states = [("the live state", self.start_state)]
        states += [(f"fixture {name}", state) for name, state in fixtures.items()]
        states += [
            (f"checkpoint {e['data']['file']}", (self.run_dir / "checkpoints" / e["data"]["file"]).read_bytes())
            for e in self.checkpoints()[-2:]
        ]
        trial = self.game_class(
            self.live.start(), self.live.adapter, registry=self.live.registry, library=self.live.library
        )
        try:
            report = validation.validate(trial, manifest, files, fixtures, states, scope, session, observations, taken)
        finally:
            trial.emu.close()
        self.reports.append(report)
        return report


def _check_name(name: str) -> None:
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ValueError("names are 2-40 characters: lowercase letters, digits and _, starting with a letter")


def describe_report(report: dict) -> str:
    verdict = (
        f"PASSED: staged as version {report['version']}; it becomes active at the next play session"
        if report["passed"]
        else "FAILED"
    )
    steps = [
        f"- {s['step']}: {'ok' if s['ok'] else 'FAILED'}" + (f" ({s['detail']})" if s["detail"] else "")
        for s in report["steps"]
    ]
    return f"Validation of {report['name']}: {verdict}\n" + "\n".join(steps)


# ---- policy ----


def tokens(usage: dict | None) -> int:
    """Tokens that count toward the development share: input, cache creation and output (cache reads excluded)."""
    usage = usage or {}
    return sum(usage.get(key) or 0 for key in ("input_tokens", "cache_creation_input_tokens", "output_tokens"))


def denies(memory, request: dict, share: float, attempts: int) -> str | None:
    """Why the policy refuses a development session for `request`, or None. The first cycle is always allowed."""
    cycles = memory.events("development_cycle", limit=-1)
    if not cycles:
        return None
    done = sum(1 for cycle in cycles if cycle["data"]["tool"] == request["tool"])
    if done >= attempts:
        return f"{request['tool']} already had {done} development session(s), the limit per tool"
    ends = memory.events("session_end", limit=-1)
    total = sum(tokens(e["data"].get("usage")) for e in ends)
    dev = sum(tokens(e["data"].get("usage")) for e in ends if e["data"].get("mode") == "develop")
    if total and dev / total > share:
        return f"development has used {dev / total:.0%} of this run's tokens; the limit is {share:.0%}"
    return None


def brief(memory, request: dict, bench: Bench, objective: str, attempt: int, attempts: int) -> str:
    evidence = {e["id"]: e for e in memory.events("action", limit=-1) if e["id"] in set(request["evidence"])}
    lines = [
        f"Objective of the run: {objective}",
        "",
        f"Development request #{request['id']} (attempt {attempt} of {attempts}) for the tool `{request['tool']}`:",
        f"Problem: {request['problem']}",
        "Evidence:",
        *[f"  [event {i}] {evidence[i]['data']['outcome']}" for i in request["evidence"] if i in evidence],
        f"Expected benefit: {request['expected_benefit']}",
        f"Proposal: {request['proposal']}",
        "",
    ]
    try:
        current = bench.live.registry.get(request["tool"], "tool")
        lines.append(f"`{request['tool']}` version {current['version']} is active: library_read shows its code.")
    except capabilities.CapabilityError:
        lines.append(f"`{request['tool']}` does not exist yet.")
    lines += ["", "Workspace:", bench.ws_list(), "", "Your observations, macros and tools:"]
    lines += [capabilities.summary(bench.live.registry, c) for c in bench.live.registry.active()] or ["  none"]
    plays = [e for e in memory.events("session_end", limit=10) if e["data"].get("mode", "play") == "play"]
    if plays:
        lines += ["", "Recent play sessions (actions, cost):"]
        lines += [
            f"  session {e['session']}: {e['data']['actions']} actions, ${e['data']['cost_usd']:.2f}" for e in plays
        ]
    lines += ["", "The live game as this session starts (the development game is a copy):", bench.live.state_text()]
    return "\n".join(lines)
