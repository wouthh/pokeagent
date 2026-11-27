"""Declarative capabilities the agent defines while playing: RAM observations and macros.

Both are plain data that this trusted module validates and interprets; no agent-written code runs. An observation
decodes a few bytes of RAM (read-only, RAM regions only) into a value the agent sees in get_state and can test in
macro conditions. A macro is a list of steps over the builtin actions (and other macros) with optional `repeat`,
`until` and `while`, bounded per call. Definitions are versioned in the memory store; a version that fails often is
rolled back to the one before it.
"""

import json
import re
import time

import jsonschema

from pokeagent import objectives
from pokeagent.memory import Memory

DECODES = ("u8", "u16le", "u16be", "bcd", "bit", "popcount", "bytes", "text")
ACTIONS = ("press", "walk", "walk_to", "advance_text", "macro")
SCOPES = ("rom", "family")
NAME = re.compile(r"[a-z][a-z0-9_]{1,39}")
MAX_READ = 64  # bytes per observation
MAX_STEPS = 20  # steps per macro
MACRO_ACTIONS = 200  # builtin actions per macro call, nested macros included
MACRO_WORK = 5000  # loop iterations and nested calls per macro call, so loops that do no action are bounded too
MAX_DEPTH = 4  # macros calling macros
ROLLBACK = (3, 10)  # failures within the last calls of a version that roll it back

SCHEMA = """
CREATE TABLE IF NOT EXISTS capabilities (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    version INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('observation', 'macro')),
    scope TEXT NOT NULL,
    spec TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'previous', 'retired')),
    run TEXT NOT NULL,
    session INTEGER,
    created REAL NOT NULL,
    UNIQUE (name, scope, version)
);
CREATE TRIGGER IF NOT EXISTS capabilities_fixed
BEFORE UPDATE OF id, name, version, kind, scope, spec, run, session, created ON capabilities
BEGIN SELECT RAISE(ABORT, 'a capability version never changes; define a new version'); END;
CREATE TRIGGER IF NOT EXISTS capabilities_kept BEFORE DELETE ON capabilities
BEGIN SELECT RAISE(ABORT, 'capability versions are never deleted'); END;
"""


class CapabilityError(ValueError):
    """A definition that does not validate, or a call that failed."""


# ---- observations ----


def check_observation(spec: dict, taken: set[str]) -> dict:
    """The normalised observation, or CapabilityError."""
    _fields(spec, {"name", "description", "address", "length", "decode", "bit", "show"})
    name = _name(spec, taken)
    address, length, kind = spec.get("address"), spec.get("length", 1), spec.get("decode")
    if not isinstance(address, int) or not 0 <= address <= 0xFFFF:
        raise CapabilityError("address must be a number 0..0xFFFF")
    if not isinstance(length, int) or not 1 <= length <= MAX_READ:
        raise CapabilityError(f"length must be 1..{MAX_READ}")
    if kind not in DECODES:
        raise CapabilityError(f"decode must be one of {', '.join(DECODES)}")
    if kind in ("u16le", "u16be") and length != 2:
        raise CapabilityError(f"{kind} reads exactly 2 bytes")
    bit = spec.get("bit")
    if (kind == "bit") != (bit is not None) or (bit is not None and not (isinstance(bit, int) and 0 <= bit <= 7)):
        raise CapabilityError("decode bit needs bit 0..7 (and only bit uses it)")
    return {
        "name": name,
        "description": _description(spec),
        "address": address,
        "length": length,
        "decode": kind,
        "bit": bit,
        "show": bool(spec.get("show", True)),
    }


def decode(spec: dict, data: bytes, text) -> int | bool | str:
    """Decode an observation's bytes; `text` is the game's own text decoder."""
    match spec["decode"]:
        case "u8":
            return data[0]
        case "u16le":
            return int.from_bytes(data, "little")
        case "u16be":
            return int.from_bytes(data, "big")
        case "bcd":
            digits = data.hex()
            return int(digits) if digits.isdigit() else -1
        case "bit":
            return bool(data[0] >> spec["bit"] & 1)
        case "popcount":
            return sum(bin(b).count("1") for b in data)
        case "bytes":
            return data.hex(" ")
        case _:
            return text(data)


# ---- macros ----


def check_macro(spec: dict, observations: set[str], macros: set[str], taken: set[str]) -> dict:
    """The normalised macro, or CapabilityError. `observations` may appear in conditions; `macros` may be called."""
    _fields(spec, {"name", "description", "params", "steps"})
    name = _name(spec, taken)
    params = spec.get("params", {"type": "object", "properties": {}})
    try:
        jsonschema.Draft202012Validator.check_schema(params)
    except jsonschema.SchemaError as e:
        raise CapabilityError(f"params is not a valid JSON schema: {e.message}") from None
    if _has_ref(params):
        raise CapabilityError("params may not use $ref or $dynamicRef: the orchestrator never fetches schemas")
    if params.get("type") != "object":
        raise CapabilityError('params must be a JSON schema of "type": "object"')
    steps = spec.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        raise CapabilityError(f"steps must be a list of 1..{MAX_STEPS} steps")
    arguments = set(params.get("properties", {}))
    for number, step in enumerate(steps, 1):
        try:
            _check_step(step, observations, macros | {name}, arguments)
        except (CapabilityError, ValueError) as e:
            raise CapabilityError(f"step {number}: {e}") from None
    return {"name": name, "description": _description(spec), "params": params, "steps": steps}


def _check_step(step, observations: set[str], macros: set[str], arguments: set[str]) -> None:
    if not isinstance(step, dict):
        raise CapabilityError("a step is an object")
    _fields(step, {"do", "args", "repeat", "until", "while", "max"})
    if step.get("do") not in ACTIONS:
        raise CapabilityError(f"do must be one of {', '.join(ACTIONS)}")
    args = step.get("args", {})
    if not isinstance(args, dict):
        raise CapabilityError("args is an object")
    for value in _arg_refs(args):
        if value not in arguments:
            raise CapabilityError(f"{{'$arg': {value!r}}} is not one of the macro's params")
    if step["do"] == "macro" and args.get("name") not in macros:
        raise CapabilityError(f"macro {args.get('name')!r} is not defined")
    loops = [key for key in ("until", "while") if key in step]
    for key in loops:
        objectives.validate(step[key], sorted(observations))
    if loops:
        if "repeat" in step:
            raise CapabilityError("use max, not repeat, with until or while")
        if not isinstance(step.get("max"), int) or not 1 <= step["max"] <= MACRO_ACTIONS:
            raise CapabilityError(f"until and while need max 1..{MACRO_ACTIONS}")
    elif "max" in step:
        raise CapabilityError("max goes with until or while")
    elif not isinstance(step.get("repeat", 1), int) or not 1 <= step.get("repeat", 1) <= MACRO_ACTIONS:
        raise CapabilityError(f"repeat must be 1..{MACRO_ACTIONS}")


def _has_ref(schema) -> bool:
    if isinstance(schema, dict):
        return any(k in ("$ref", "$dynamicRef") or _has_ref(v) for k, v in schema.items())
    return isinstance(schema, list) and any(_has_ref(v) for v in schema)


def _arg_refs(value):
    if isinstance(value, dict):
        if set(value) == {"$arg"}:
            yield value["$arg"]
        else:
            for item in value.values():
                yield from _arg_refs(item)
    elif isinstance(value, list):
        for item in value:
            yield from _arg_refs(item)


def _substitute(value, args: dict):
    if isinstance(value, dict):
        if set(value) == {"$arg"}:
            if value["$arg"] not in args:
                raise CapabilityError(f"argument {value['$arg']!r} was not given")
            return args[value["$arg"]]
        return {key: _substitute(item, args) for key, item in value.items()}
    if isinstance(value, list):
        return [_substitute(item, args) for item in value]
    return value


def check_args(spec: dict, args: dict) -> None:
    try:
        jsonschema.validate(args, spec["params"])
    except jsonschema.ValidationError as e:
        raise CapabilityError(f"{spec['name']}: arguments do not match its params: {e.message}") from None


def _holds(condition: dict, observe) -> bool:
    try:
        return objectives.holds(condition, observe())
    except (TypeError, KeyError) as e:  # e.g. a text observation compared with a number, or one this ROM lacks
        raise CapabilityError(f"condition {objectives.describe(condition)} cannot be evaluated: {e!r}") from None


def execute(spec: dict, args: dict, act, observe, lookup, budget: list[int] | None = None, depth: int = 0) -> list[str]:
    """Run a macro: `act(name, args)` runs one builtin action and returns its outcome, `observe()` returns the
    observations for conditions and `lookup(name)` an active macro. Returns the outcomes; CapabilityError on failure."""
    check_args(spec, args)
    budget = budget if budget is not None else [MACRO_ACTIONS, MACRO_WORK]
    outcomes = []
    for number, step in enumerate(spec["steps"], 1):
        step_args = _substitute(step.get("args", {}), args)
        times = step.get("max") or step.get("repeat", 1)
        for _ in range(times):
            if budget[1] <= 0:
                raise CapabilityError(f"{spec['name']}: a macro call may take at most {MACRO_WORK} steps in total")
            budget[1] -= 1
            if "while" in step and not _holds(step["while"], observe):
                break
            if step["do"] == "macro":
                if depth + 1 > MAX_DEPTH:
                    raise CapabilityError(f"{spec['name']}: macros nest at most {MAX_DEPTH} deep")
                inner = lookup(step_args["name"])
                outcomes += execute(inner, step_args.get("args", {}), act, observe, lookup, budget, depth + 1)
            else:
                if budget[0] <= 0:
                    raise CapabilityError(f"{spec['name']}: a macro call may take at most {MACRO_ACTIONS} actions")
                budget[0] -= 1
                outcomes.append(act(step["do"], step_args))
            if "until" in step and _holds(step["until"], observe):
                break
        else:
            if "until" in step:
                raise CapabilityError(f"{spec['name']}: step {number}'s until did not hold after {times} times")
    return outcomes


# ---- registry ----


class Registry:
    """Capability versions in the memory store: one active version per name and scope; later runs on the same ROM
    (rom scope) or game family (family scope) find them."""

    def __init__(self, memory: Memory):
        self.memory = memory
        with memory.lock:
            memory.db.executescript(SCHEMA)

    def _scopes(self) -> list[str]:
        return [self.memory.scopes["rom"], self.memory.scopes["family"]]

    def active(self, kind: str | None = None) -> list[dict]:
        """Active capabilities this run can see; a ROM-scoped one hides a family-scoped one of the same name."""
        rows = self.memory.rows(
            "SELECT * FROM capabilities WHERE status = 'active' AND scope IN (?, ?) ORDER BY scope = ? DESC, name",
            *self._scopes(),
            self.memory.scopes["rom"],
        )
        found: dict[str, dict] = {}
        for row in rows:
            if kind in (None, row["kind"]):
                found.setdefault(row["name"], dict(row) | {"spec": json.loads(row["spec"])})
        return sorted(found.values(), key=lambda c: c["name"])

    def get(self, name: str, kind: str) -> dict:
        found = next((c for c in self.active(kind) if c["name"] == name), None)
        if found is None:
            raise CapabilityError(f"no active {kind} named {name!r}")
        return found

    def define(self, kind: str, spec: dict, scope: str, session: int | None) -> dict:
        """Store a validated spec as the new active version of its name in `scope`."""
        if scope not in SCOPES:
            raise CapabilityError(f"scope must be one of {', '.join(SCOPES)}")
        where = (spec["name"], self.memory.scopes[scope])
        with self.memory.transaction() as db:
            version = db.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM capabilities WHERE name = ? AND scope = ?", where
            ).fetchone()[0]
            db.execute(
                "UPDATE capabilities SET status = 'previous' WHERE name = ? AND scope = ? AND status = 'active'", where
            )
            db.execute(
                "INSERT INTO capabilities (name, version, kind, scope, spec, status, run, session, created)"
                " VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)",
                (*where[:1], version, kind, where[1], json.dumps(spec), self.memory.run, session, time.time()),
            )
        self.memory.event("capability_defined", {"name": spec["name"], "version": version, "kind": kind}, session)
        return self.get(spec["name"], kind)

    def record(self, capability: dict, error: str | None, session: int | None) -> str | None:
        """Record one call; roll the version back after too many recent failures. Returns a note when it did."""
        call = {"id": capability["id"], "name": capability["name"], "version": capability["version"], "error": error}
        self.memory.event("capability_call", call, session)
        failures, window = ROLLBACK
        recent = self.memory.capability_calls(capability["id"])
        if error and sum(1 for e in recent[-window:] if e["data"]["error"]) >= failures:
            return self.rollback(capability, f"{failures} of its last {window} calls failed", session)
        return None

    def rollback(self, capability: dict, reason: str, session: int | None = None) -> str:
        """Retire this version; the newest earlier version of the same name and scope becomes active again."""
        where = (capability["name"], capability["scope"])
        with self.memory.transaction() as db:
            db.execute("UPDATE capabilities SET status = 'retired' WHERE id = ?", (capability["id"],))
            earlier = db.execute(
                "SELECT id, version FROM capabilities WHERE name = ? AND scope = ? AND status = 'previous'"
                " ORDER BY version DESC LIMIT 1",
                where,
            ).fetchone()
            if earlier:
                db.execute("UPDATE capabilities SET status = 'active' WHERE id = ?", (earlier[0],))
        back = f"version {earlier[1]} is active again" if earlier else "no earlier version exists"
        note = f"{capability['name']} version {capability['version']} was rolled back ({reason}); {back}"
        self.memory.event("capability_rollback", {"name": capability["name"], "note": note}, session)
        return note

    def usage(self, capability: dict) -> tuple[int, int]:
        """(calls, failures) of this version."""
        calls = self.memory.capability_calls(capability["id"])
        return len(calls), sum(1 for e in calls if e["data"]["error"])


def summary(registry: Registry, capability: dict) -> str:
    calls, failures = registry.usage(capability)
    spec = capability["spec"]
    used = f"{calls} calls, {failures} failed" if calls else "unused"
    scope = capability["scope"].split(":")[0]
    if capability["kind"] == "observation":
        where = f"{spec['length']} byte(s) at {spec['address']:#06x} as {spec['decode']}"
        return f"{spec['name']} (observation v{capability['version']}, {scope}; {where}): {spec['description']}"
    params = ", ".join(spec["params"].get("properties", {})) or "no params"
    return f"{spec['name']} (macro v{capability['version']}, {scope}; {params}; {used}): {spec['description']}"


# ---- shared checks ----


def _fields(spec, allowed: set[str]) -> None:
    if not isinstance(spec, dict):
        raise CapabilityError("a definition is an object")
    if extra := set(spec) - allowed:
        raise CapabilityError(f"unknown fields {sorted(extra)}; allowed: {sorted(allowed)}")


def _name(spec: dict, taken: set[str]) -> str:
    name = spec.get("name")
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise CapabilityError("name must be 2-40 characters: lowercase letters, digits and _, starting with a letter")
    if name in taken:
        raise CapabilityError(f"{name} is already a builtin name")
    return name


def _description(spec: dict) -> str:
    text = spec.get("description")
    if not isinstance(text, str) or not text.strip() or len(text) > 300:
        raise CapabilityError("description must be 1-300 characters")
    return text.strip()
