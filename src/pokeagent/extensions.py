"""Code extensions: agent-written Python tools run in the sandbox and held to trusted contracts.

An extension is a manifest plus `tool.py` with `run(ctx, **params)`. Its code runs only in `extension_host` inside
bubblewrap (no network, no home, no ROM, rlimits, a wall-clock deadline) and reaches the game only through `ctx`
requests, which this module serves under the game's lock. The orchestrator, not the extension, enforces the contract:

- before the call: arguments against `params`, then `preconditions` (a call whose preconditions fail is refused and
  not counted against the tool);
- during it: one budget of frames, controller inputs and wall time shared by every nested `ctx.call`, at most 4
  nested calls deep, and the `invariants` after every request that ran the game;
- after it: the result against `returns`, the `postconditions` (which may compare with `{"before": obs}` values),
  and the standard fingerprint, where every changed group must be among the declared `effects`.

A failure or violation counts against the version like a failed macro call, and the registry rolls it back.
Files live content-addressed in `<data dir>/library/<sha256>/tool.py`, so a version never changes after it is stored.
"""

import contextlib
import copy
import hashlib
import json
import os
import select
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import jsonschema

from pokeagent import capabilities, objectives, sandbox
from pokeagent.capabilities import CapabilityError
from pokeagent.emulator.client import RequestRefused
from pokeagent.extension_host import API_VERSION

EFFECTS = ("position", "map", "party", "bag", "money", "battle", "menu")
BUTTONS = ("a", "b", "start", "select", "up", "down", "left", "right")
LIMITS = {"RLIMIT_AS": 1 << 30, "RLIMIT_NPROC": 16, "RLIMIT_FSIZE": 8 << 20, "RLIMIT_NOFILE": 64, "RLIMIT_CPU": 120}
MAX_LINE = 64 << 10  # one protocol line from the host
STORE_LIMIT = 64 << 10  # bytes of JSON per kv or tkv namespace
MAX_LOG = 50  # ctx.log lines kept per call
MAX_DEPTH = 4
MAX_SOURCE = 64 << 10
MAX_EXAMPLES = 5


class Refused(CapabilityError):
    """A call refused before it ran (arguments or preconditions); it does not count against the tool."""


@dataclass
class Budget:
    """Shared by a top-level call and everything it calls."""

    frames: int = 18_000  # five emulated minutes
    inputs: int = 600
    seconds: float = 60
    deadline: float = 0.0
    frame_end: int = 0
    logs: list[str] = field(default_factory=list)

    @classmethod
    def start(cls, game, **limits) -> "Budget":
        budget = cls(**limits)
        budget.deadline, budget.frame_end = time.monotonic() + budget.seconds, game.frames + budget.frames
        return budget


# ---- manifests ----


def check_manifest(manifest: dict, observations: set[str], tools: set[str], taken: set[str], family: str) -> dict:
    """Validate a manifest; return it normalised. `observations` are the names conditions may use, `tools` the
    capabilities it may require."""
    allowed = {"name", "description", "params", "returns", "limitations", "compatibility", "requires"}
    allowed |= {"preconditions", "postconditions", "invariants", "effects", "prefer_over", "api_version", "examples"}
    capabilities._fields(manifest, allowed)
    out = {"name": capabilities._name(manifest, taken), "description": capabilities._description(manifest)}
    for key in ("params", "returns"):
        schema = manifest.get(key, {"type": "object"} if key == "params" else {})
        try:
            jsonschema.Draft202012Validator.check_schema(schema)
        except jsonschema.SchemaError as e:
            raise CapabilityError(f"{key} is not a valid JSON schema: {e.message}") from None
        if capabilities._has_ref(schema):
            raise CapabilityError(f"{key} may not use $ref or $dynamicRef: the orchestrator never fetches schemas")
        if key == "params" and schema.get("type") != "object":
            raise CapabilityError('params must be an object schema ({"type": "object", ...})')
        out[key] = schema
    if manifest.get("api_version", API_VERSION) != API_VERSION:
        raise CapabilityError(f"api_version must be {API_VERSION}")
    limitations = manifest.get("limitations", "")
    if not isinstance(limitations, str) or len(limitations) > 1000:
        raise CapabilityError("limitations must be text of at most 1000 characters")
    compatibility = manifest.get("compatibility", {"family": family})
    if not isinstance(compatibility, dict) or set(compatibility) - {"roms", "family"}:
        raise CapabilityError('compatibility is {"family": ..., "roms": [sha1, ...]}')
    if compatibility.get("family", family) != family:
        raise CapabilityError(f"this game's family is {family}")
    requires = manifest.get("requires", [])
    if not isinstance(requires, list) or set(requires) - tools:
        raise CapabilityError(f"requires lists capabilities that do not exist: {sorted(set(requires) - tools)}")
    effects = manifest.get("effects", [])
    if not isinstance(effects, list) or set(effects) - set(EFFECTS):
        raise CapabilityError(f"effects must be a list of {', '.join(EFFECTS)}")
    for key in ("preconditions", "invariants", "postconditions"):
        if key in manifest:
            _check_condition(manifest[key], observations, before=key == "postconditions")
            out[key] = manifest[key]
    prefer = manifest.get("prefer_over")
    if prefer is not None and prefer not in capabilities.ACTIONS[:-1]:
        raise CapabilityError(f"prefer_over names a builtin action: {', '.join(capabilities.ACTIONS[:-1])}")
    examples = manifest.get("examples", [{}])
    if not isinstance(examples, list) or not 1 <= len(examples) <= MAX_EXAMPLES:
        raise CapabilityError(f"examples is a list of 1-{MAX_EXAMPLES} argument sets for the validation runs")
    for example in examples:
        try:
            jsonschema.validate(example, out["params"])
        except jsonschema.ValidationError as e:
            raise CapabilityError(f"example {example!r} does not match params: {e.message}") from None
    return out | {
        "examples": examples,
        "limitations": limitations,
        "compatibility": compatibility,
        "requires": requires,
        "effects": effects,
        "prefer_over": prefer,
        "api_version": API_VERSION,
    }


def _check_condition(condition, names: set[str], before: bool) -> None:
    refs = set()

    def strip(c):
        if isinstance(c, dict) and isinstance(c.get("value"), dict):
            if not before or set(c["value"]) != {"before"}:
                raise CapabilityError(f"a value is a number{' or {"before": observation}' if before else ''}")
            refs.add(c["value"]["before"])
            return c | {"value": 0}
        if isinstance(c, dict) and ("all" in c or "any" in c):
            key = "all" if "all" in c else "any"
            return {key: [strip(part) for part in c[key]]} if isinstance(c[key], list) else c
        if isinstance(c, dict) and "not" in c:
            return {"not": strip(c["not"])}
        return c

    try:
        objectives.validate(strip(condition), sorted(names))
    except ValueError as e:
        raise CapabilityError(str(e)) from None
    if unknown := refs - names:
        raise CapabilityError(f"before names unknown observations: {sorted(unknown)}")


def _resolve(condition, before: dict):
    """The condition with every {"before": obs} value replaced by that observation's value before the call."""
    if isinstance(condition, dict):
        if isinstance(condition.get("value"), dict):
            return condition | {"value": before[condition["value"]["before"]]}
        return {key: _resolve(value, before) for key, value in condition.items()}
    if isinstance(condition, list):
        return [_resolve(part, before) for part in condition]
    return condition


def store(library: Path, source: str) -> str:
    """Put tool.py into the content-addressed library; return its sha256."""
    data = source.encode()
    if len(data) > MAX_SOURCE:
        raise CapabilityError(f"tool.py is limited to {MAX_SOURCE} bytes")
    digest = hashlib.sha256(data).hexdigest()
    folder = library / digest
    if not (folder / "tool.py").exists():
        tmp = library / f".{digest}.tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, mode=0o700)
        (tmp / "tool.py").write_bytes(data)
        tmp.rename(folder)
    return digest


# ---- running ----


class Runner:
    """Runs tools on one game, holding its lock. `game` provides the adapter, `_do`, `run`, `frames`,
    `_observations`, `registry`, `emu`, `library` and `tkv` (the timeline store's path). `fixtures` (name -> state)
    are served to the agent's tests during validation only."""

    def __init__(self, game, fixtures: dict[str, bytes] | None = None):
        self.game, self.fixtures = game, fixtures

    def run_tests(self, capability: dict, folder: Path, budget: Budget) -> list[dict]:
        """Run the agent's own tests (folder/test_tool.py) in the sandbox; return one result per test."""
        results = self._host(capability, folder, ["--tests"], budget, 0)
        if not isinstance(results, list):
            raise CapabilityError("the test run broke the protocol")
        return results

    def call(self, capability: dict, args: dict, budget: Budget | None = None, depth: int = 0):
        """Run one tool call; return its result. Refused, CapabilityError (a failure) or a game error."""
        game, spec = self.game, capability["spec"]
        try:
            jsonschema.validate(args, spec["params"])
        except jsonschema.ValidationError as e:
            raise Refused(f"{spec['name']}: arguments do not match its params: {e.message}") from None
        rom = game.registry.memory.scopes["rom"].split(":", 1)[1]
        if (roms := spec["compatibility"].get("roms")) and rom not in roms:  # a family-scoped tool can reach other ROMs
            raise Refused(f"{spec['name']}: this ROM is not in the tool's compatibility list")
        before_obs = game._observations()
        if "preconditions" in spec and not objectives.holds(spec["preconditions"], before_obs):
            raise Refused(
                f"{spec['name']}: its preconditions do not hold now ({objectives.describe(spec['preconditions'])})"
            )
        budget = budget or Budget.start(game)
        before = game.adapter.state(game)
        result = self._run_host(capability, args, budget, depth)
        after = game.adapter.state(game)
        try:
            jsonschema.validate(result, spec["returns"])
        except jsonschema.ValidationError as e:
            raise CapabilityError(f"{spec['name']}: its result does not match returns: {e.message}") from None
        old, new = game.adapter.fingerprint(before), game.adapter.fingerprint(after)
        if undeclared := [g for g in new if new[g] != old[g] and g not in spec["effects"]]:
            raise CapabilityError(f"{spec['name']} changed {', '.join(undeclared)}, which its effects do not declare")
        if "postconditions" in spec:
            condition = _resolve(spec["postconditions"], before_obs)
            if not objectives.holds(condition, game._observations()):
                raise CapabilityError(f"{spec['name']}: postcondition failed ({objectives.describe(condition)})")
        return result

    def _run_host(self, capability: dict, args: dict, budget: Budget, depth: int):
        folder = self.game.library / capability["spec"]["sha256"]
        return self._host(capability, folder, [json.dumps(args)], budget, depth)

    def _host(self, capability: dict, folder: Path, argv: list[str], budget: Budget, depth: int):
        spec = capability["spec"]
        proc = sandbox.spawn(
            "pokeagent.extension_host",
            argv,
            {"/ext": folder},
            LIMITS,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        pipe = _Pipe(proc, budget.deadline)
        try:
            while True:
                message = pipe.receive()
                if "done" in message:
                    if not message["done"]:
                        raise CapabilityError(f"{spec['name']} failed: {str(message.get('error'))[:500]}")
                    return message.get("result")
                try:
                    reply = {"ok": True, "result": self._serve(capability, message, budget, depth)}
                except _Violation:
                    raise
                except (ValueError, KeyError, TypeError) as e:  # a refused request: the extension may handle it
                    reply = {"ok": False, "error": str(e)[:500]}
                pipe.send(reply)
        finally:
            sandbox.kill(proc)
            proc.stdin.close()
            proc.stdout.close()

    def _serve(self, capability: dict, message: dict, budget: Budget, depth: int):
        game, spec = self.game, capability["spec"]
        method, args = message.get("call"), message.get("args")
        if not isinstance(args, dict):
            raise ValueError("args must be an object")
        match method:
            case "state":
                return game.adapter.state(game)
            case "observe":
                return game._observations()[args["name"]]
            case "screen_text":
                return game.adapter.state(game)["screen_text"]
            case "read":
                length = args.get("length", 1)
                if not isinstance(length, int) or not 1 <= length <= capabilities.MAX_READ:
                    raise ValueError(f"length must be 1..{capabilities.MAX_READ}")
                try:
                    return game.emu.read(args["address"], length).hex()
                except RequestRefused as e:
                    raise ValueError(str(e)) from None
            case "load_fixture":
                if self.fixtures is None or args.get("name") not in self.fixtures:
                    raise ValueError(f"no fixture named {args.get('name')!r}; fixtures exist only in validation tests")
                game.emu.load_state(self.fixtures[args["name"]])
                return None
            case "log":
                if len(budget.logs) < MAX_LOG:
                    budget.logs.append(f"{spec['name']}: {str(args.get('message'))[:500]}")
                return None
            case "kv_get" | "kv_set" | "tkv_get" | "tkv_set":
                return self._store(capability, method, args)
            case "press" | "hold" | "wait" | "call":
                try:
                    result = self._act(method, args, budget, depth)
                    self._spend(budget, 0)  # a compound builtin may have run past the frame limit
                except _Violation:
                    raise
                except Exception:  # a failed request may have changed the game first: the contract still holds
                    self._check_invariants(spec, game)
                    raise
                self._check_invariants(spec, game)
                return result
        raise ValueError(f"unknown ctx request {method!r}")

    @staticmethod
    def _check_invariants(spec: dict, game) -> None:
        if "invariants" not in spec:
            return
        try:
            held = objectives.holds(spec["invariants"], game._observations())
        except (TypeError, KeyError) as e:  # unevaluable (type mismatch, observation missing on this ROM): not held
            raise _Violation(f"{spec['name']}: invariant cannot be evaluated ({e!r})") from None
        if not held:
            raise _Violation(f"{spec['name']}: invariant failed ({objectives.describe(spec['invariants'])})")

    def _charge(self, action: str, args: dict, budget: Budget):
        """One builtin action of a macro, under the tool's shared budget."""
        self._spend(budget, _cost(action, args))
        result = self.game._do(action, args)
        self._spend(budget, 0)
        return result

    def _spend(self, budget: Budget, inputs: int, frames: int = 0) -> None:
        if time.monotonic() > budget.deadline or self.game.frames + frames > budget.frame_end:
            raise _Violation("the call's budget of frames or time is used up")
        budget.inputs -= inputs
        if budget.inputs < 0:
            raise _Violation("the call's budget of controller inputs is used up")

    def _act(self, method: str, args: dict, budget: Budget, depth: int):
        game = self.game
        if method != "call":
            frames = args.get("frames")
            self._spend(
                budget, _cost(method, args), frames if method in ("hold", "wait") and isinstance(frames, int) else 0
            )
        match method:
            case "press":
                return game._do("press", {"button": args.get("button"), "times": args.get("times", 1)})
            case "hold" | "wait":
                buttons, frames = args.get("buttons", []) if method == "hold" else [], args.get("frames")
                if not isinstance(buttons, list) or set(buttons) - set(BUTTONS):
                    raise ValueError(f"buttons must be a list of {', '.join(BUTTONS)}")
                if not isinstance(frames, int) or not 1 <= frames <= 600:
                    raise ValueError("frames must be 1..600")
                game.run(buttons, frames)
                return None
        name, call_args = args.get("name"), args.get("args", {})
        if name in capabilities.ACTIONS[:-1] and isinstance(call_args, dict):
            self._spend(budget, _cost(name, call_args))  # a builtin costs its own input count
        else:
            self._spend(budget, 1)
        if not isinstance(call_args, dict):
            raise ValueError("args must be an object")
        if depth + 1 > MAX_DEPTH:
            raise _Violation(f"capabilities may call each other at most {MAX_DEPTH} deep")
        if name in capabilities.ACTIONS[:-1]:
            return game._do(name, call_args)
        found = next((c for c in game.registry.active() if c["name"] == name), None)
        if found is None:
            raise ValueError(f"no builtin action or active capability named {name!r}")
        if found["kind"] == "macro":
            return capabilities.execute(
                found["spec"],
                call_args,
                lambda action, action_args: self._charge(action, action_args, budget),
                game._observations,
                lambda n: game.registry.get(n, "macro")["spec"],
            )
        if found["kind"] == "observation":
            raise ValueError(f"{name} is an observation; use ctx.observe")
        try:
            return self.call(found, call_args, budget, depth + 1)
        except _Violation:
            raise
        except CapabilityError as e:  # the inner tool failed; the outer one may handle that
            raise ValueError(str(e)) from None

    def _store(self, capability: dict, method: str, args: dict):
        key = args.get("key")
        if not isinstance(key, str) or not 1 <= len(key) <= 200:
            raise ValueError("a key is 1-200 characters of text")
        durable = method.startswith("kv")
        data = self.game.registry.kv(capability) if durable else _read_json(self.game.tkv).get(capability["name"], {})
        if method.endswith("_get"):
            return {"value": data[key]} if key in data else None
        data = copy.deepcopy(data)
        if args.get("value") is None:
            data.pop(key, None)
        else:
            data[key] = args["value"]
        if len(json.dumps(data)) > STORE_LIMIT:
            raise ValueError(f"a store holds at most {STORE_LIMIT} bytes of JSON")
        if durable:
            self.game.registry.set_kv(capability, data)
        else:
            stores = _read_json(self.game.tkv) | {capability["name"]: data}
            tmp = self.game.tkv.with_suffix(".tmp")
            tmp.write_text(json.dumps(stores))
            tmp.replace(self.game.tkv)
        return None


def _cost(action: str, args: dict) -> int:
    times = args.get("times", 1)
    return times if action == "press" and isinstance(times, int) and times > 0 else 1


class _Violation(CapabilityError):
    """Ends the call at once: the extension cannot catch it as a refused request."""


def _read_json(path: Path) -> dict:
    with contextlib.suppress(FileNotFoundError):
        return json.loads(path.read_text())
    return {}


class _Pipe:
    """Line protocol with the host under the call's deadline; every line is untrusted."""

    def __init__(self, proc: subprocess.Popen, deadline: float):
        self.proc, self.deadline, self.buf = proc, deadline, bytearray()
        os.set_blocking(proc.stdin.fileno(), False)

    def _wait(self, read: bool) -> None:
        fd = self.proc.stdout.fileno() if read else self.proc.stdin.fileno()
        remaining = self.deadline - time.monotonic()
        if remaining <= 0 or not any(select.select([fd] if read else [], [] if read else [fd], [], remaining)):
            raise _Violation("the call ran out of time and was stopped")

    def receive(self) -> dict:
        while (end := self.buf.find(b"\n")) < 0:
            if len(self.buf) > MAX_LINE:
                raise _Violation("the extension sent an oversized message")
            self._wait(read=True)
            chunk = os.read(self.proc.stdout.fileno(), 1 << 16)
            if not chunk:
                try:
                    code = self.proc.wait(max(self.deadline - time.monotonic(), 0))
                except subprocess.TimeoutExpired:
                    raise _Violation("the call ran out of time and was stopped") from None
                raise CapabilityError(f"the extension's process exited ({code}) without a result")
            self.buf += chunk
        line = bytes(self.buf[:end])
        del self.buf[: end + 1]
        try:
            message = json.loads(line)
        except ValueError:
            raise _Violation("the extension broke the protocol") from None
        if not isinstance(message, dict):
            raise _Violation("the extension broke the protocol")
        return message

    def send(self, message: dict) -> None:
        out = memoryview(json.dumps(message, default=str).encode() + b"\n")
        try:
            while out:
                self._wait(read=False)
                with contextlib.suppress(BlockingIOError):
                    out = out[os.write(self.proc.stdin.fileno(), out) :]
        except BrokenPipeError:
            raise CapabilityError("the extension's process exited without a result") from None


def install(
    registry, library: Path, manifest: dict, source: str, scope: str, session, observations: set[str], taken: set[str]
) -> dict:
    """Check a manifest, store its code and make it the active version of its name. Validation runs before this."""
    tools = {c["name"] for c in registry.active()}
    family = registry.memory.scopes["family"].split(":", 1)[1]
    spec = check_manifest(manifest, observations, tools, taken, family)
    rom = registry.memory.scopes["rom"].split(":", 1)[1]
    if spec["compatibility"].get("roms") and rom not in spec["compatibility"]["roms"]:
        raise CapabilityError("this ROM is not in the tool's compatibility list")
    return registry.define("tool", spec | {"sha256": store(library, source)}, scope, session)
