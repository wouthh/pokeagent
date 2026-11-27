"""Validation of a code extension before it is staged. The orchestrator owns every step; the agent can ask for a
validation but cannot change it, and its own tests alone never pass one.

1. The manifest: schemas, compatibility, conditions and the example argument sets.
2. ruff's serious checks (syntax errors, undefined names) on tool.py and test_tool.py; ruff only reads them.
3. The agent's tests (`test_*(ctx)` in test_tool.py) in the sandbox, on the validation game, with its fixtures.
4. Trusted contract runs: every example on up to three states (the live state first, then fixtures and earlier
   checkpoints) where the preconditions hold. At least one state must run, and no run may fail.

The report is a `validation` event. A tool that passes is staged and becomes active at the next session boundary,
after a checkpoint. Validation uses its own emulator, a scratch timeline store and a scratch copy of the tools'
durable stores, so it changes nothing the run keeps.
"""

import subprocess
import sys
import tempfile
from pathlib import Path

from pokeagent import extensions
from pokeagent.capabilities import SCOPES, CapabilityError
from pokeagent.emulator.client import EmulatorError

MAX_STATES = 3
RUFF = [sys.executable, "-m", "ruff", "check", "--isolated", "--no-cache", "--output-format", "concise"]
RUFF += ["--select", "E9,F63,F7,F82"]  # syntax errors and undefined names
TEST_BUDGET = {"frames": 36_000, "inputs": 2_000, "seconds": 180}


class _Trial:
    """The run's registry for lookups, with the candidate in place of its active version and scratch stores."""

    def __init__(self, registry, candidate: dict):
        self.registry, self.candidate, self.memory, self.stores = registry, candidate, registry.memory, {}

    def active(self, kind: str | None = None) -> list[dict]:
        found = [c for c in self.registry.active(kind) if c["name"] != self.candidate["name"]]
        return found + ([self.candidate] if kind in (None, "tool") else [])

    def get(self, name: str, kind: str) -> dict:
        return self.candidate if (name, kind) == (self.candidate["name"], "tool") else self.registry.get(name, kind)

    def kv(self, capability: dict) -> dict:
        if capability["name"] in self.stores:
            return self.stores[capability["name"]]
        return self.registry.kv(capability)

    def set_kv(self, capability: dict, data: dict) -> None:
        self.stores[capability["name"]] = data


def validate(
    game,
    manifest: dict,
    files: dict[str, str],
    fixtures: dict[str, bytes],
    states: list[tuple[str, bytes]],
    scope: str,
    session: int | None,
    observations: set[str],
    taken: set[str],
) -> dict:
    """Validate a tool on `game` (a Game on its own emulator, used for nothing else; its `registry` is the run's).
    `states` are (label, state) pairs, the live state first. Returns the report; a passing tool is staged."""
    registry = game.registry
    report = {"name": manifest.get("name") if isinstance(manifest, dict) else None, "passed": False, "steps": []}

    def step(name: str, ok: bool, detail: str = "") -> bool:
        report["steps"].append({"step": name, "ok": ok, "detail": detail[:2000]})
        return ok

    try:
        if scope not in SCOPES:
            raise CapabilityError(f"scope must be one of {', '.join(SCOPES)}")
        tools = {c["name"] for c in registry.active()} - {manifest.get("name")}
        family = registry.memory.scopes["family"].split(":", 1)[1]
        spec = extensions.check_manifest(manifest, observations, tools, taken, family)
        rom = registry.memory.scopes["rom"].split(":", 1)[1]
        if spec["compatibility"].get("roms") and rom not in spec["compatibility"]["roms"]:
            raise CapabilityError("this ROM is not in the tool's compatibility list")
        if not isinstance(files.get("tool.py"), str) or not isinstance(files.get("test_tool.py"), str):
            raise CapabilityError("a tool needs tool.py and test_tool.py")
        spec["sha256"] = extensions.store(game.library, files["tool.py"])
    except CapabilityError as e:
        step("manifest", False, str(e))
        return _record(registry, report, session)
    step("manifest", True)
    candidate = {"id": None, "name": spec["name"], "version": 0, "kind": "tool"}
    candidate |= {"scope": registry.memory.scopes[scope], "spec": spec}

    with tempfile.TemporaryDirectory(prefix="pokeagent-validate-") as tmp:
        folder = Path(tmp)
        for name in ("tool.py", "test_tool.py"):
            (folder / name).write_text(files[name])
        lint = subprocess.run(
            [*RUFF, "tool.py", "test_tool.py"],
            cwd=folder,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if not step("ruff", lint.returncode == 0, lint.stdout or lint.stderr):
            return _record(registry, report, session)
        live_tkv = game.tkv
        baseline = live_tkv.read_bytes() if live_tkv and live_tkv.exists() else None
        game.registry, game.tkv = _Trial(registry, candidate), folder / "tkv.json"
        try:
            passed = _tests(game, candidate, folder, fixtures, states[0][1], step) and _contract(
                game, candidate, states, step, baseline
            )
        except EmulatorError as e:
            passed = step("emulator", False, f"the validation emulator failed: {e}")
        finally:
            game.registry, game.tkv = registry, live_tkv
    if passed:
        staged = registry.define("tool", spec, scope, session, staged=True)
        report |= {"passed": True, "version": staged["version"]}
    return _record(registry, report, session)


def _reset_tkv(tkv: Path, baseline: bytes | None) -> None:
    """A scratch copy of the live timeline store (absent when the live one is)."""
    if baseline is None:
        tkv.unlink(missing_ok=True)
    else:
        tkv.write_bytes(baseline)


def _tests(game, candidate: dict, folder: Path, fixtures: dict, live: bytes, step) -> bool:
    game.emu.load_state(live)
    budget = extensions.Budget.start(game, **TEST_BUDGET)
    try:
        results = extensions.Runner(game, fixtures).run_tests(candidate, folder, budget)
    except CapabilityError as e:
        return step("tests", False, str(e))
    failed = [r for r in results if not (isinstance(r, dict) and r.get("passed") is True)]
    detail = f"{len(results) - len(failed)} of {len(results)} passed" + "".join(
        f"\n{r.get('test')}: {r.get('error')}" for r in failed if isinstance(r, dict)
    )
    return step("tests", bool(results) and not failed, detail if results else "test_tool.py has no test_* function")


def _contract(game, candidate: dict, states: list[tuple[str, bytes]], step, baseline: bytes | None) -> bool:
    ran = 0
    for label, state in states:
        if ran == MAX_STATES:
            break
        outcomes = []
        for args in candidate["spec"]["examples"]:
            game.emu.load_state(state)
            game.registry.stores.clear()  # every trusted run starts from empty scratch stores
            _reset_tkv(game.tkv, baseline)
            try:
                extensions.Runner(game).call(candidate, args)
                outcomes.append("ok")
            except extensions.Refused as e:
                outcomes.append(f"refused: {e}")
            except CapabilityError as e:
                return step(f"contract on {label}", False, f"with {args}: {e}")
        if any(outcome == "ok" for outcome in outcomes):
            ran += 1
            step(f"contract on {label}", True, "; ".join(outcomes))
    return step("contract runs", ran > 0, f"ran on {ran} state(s)" if ran else "the preconditions held in no state")


def _record(registry, report: dict, session: int | None) -> dict:
    registry.memory.event("validation", report, session)
    return report
