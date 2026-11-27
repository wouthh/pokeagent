import sqlite3

import pytest

from pokeagent.capabilities import (
    MACRO_ACTIONS,
    CapabilityError,
    Registry,
    check_macro,
    check_observation,
    decode,
    execute,
)
from pokeagent.memory import Memory

BUILTIN = {"map_id", "badges", "press", "walk"}


def observation(**fields):
    return {"name": "hp_low", "description": "first party member's HP", "address": 0xD16C, "length": 2} | fields


def test_observations_validate_and_decode():
    spec = check_observation(observation(decode="u16be"), BUILTIN)
    assert spec["show"] and decode(spec, b"\x01\x02", None) == 0x0102
    assert decode(check_observation(observation(decode="u16le"), BUILTIN), b"\x01\x02", None) == 0x0201
    assert decode(check_observation(observation(length=3, decode="bcd"), BUILTIN), b"\x00\x12\x34", None) == 1234
    assert decode(check_observation(observation(length=1, decode="bit", bit=3), BUILTIN), b"\x08", None) is True
    assert decode(check_observation(observation(decode="popcount"), BUILTIN), b"\xff\x01", None) == 9
    assert decode(check_observation(observation(decode="bytes"), BUILTIN), b"\xab\xcd", None) == "ab cd"
    assert decode(check_observation(observation(decode="text"), BUILTIN), b"AB", bytes.decode) == "AB"
    bad = [
        observation(decode="float"),
        observation(decode="u16le", length=1),
        observation(decode="bit"),
        observation(decode="u8", bit=1),
        observation(decode="u8", length=65),
        observation(decode="u8", address=0x10000),
        observation(decode="u8", name="Bad Name"),
        observation(decode="u8", name="badges"),
        observation(decode="u8", description=""),
        observation(decode="u8", extra=1),
    ]
    for spec in bad:
        with pytest.raises(CapabilityError):
            check_observation(spec, BUILTIN)


def macro(steps, params=None, name="heal"):
    spec = {"name": name, "description": "heal at the counter", "steps": steps}
    return spec | ({"params": params} if params else {})


def test_macros_validate():
    params = {"type": "object", "properties": {"times": {"type": "integer", "minimum": 1}}, "required": ["times"]}
    ok = macro([{"do": "press", "args": {"button": "a", "times": {"$arg": "times"}}}], params)
    assert check_macro(ok, {"map_id"}, set(), BUILTIN)["params"] == params
    bad = [
        macro([]),
        macro([{"do": "poke", "args": {}}]),
        macro([{"do": "press", "args": {"button": {"$arg": "times"}}}]),  # no such param
        macro([{"do": "macro", "args": {"name": "elsewhere"}}]),
        macro([{"do": "walk", "until": {"obs": "map_id", "op": "==", "value": 1}}]),  # until needs max
        macro([{"do": "walk", "until": {"obs": "hp", "op": "==", "value": 1}, "max": 5}]),
        macro([{"do": "walk", "repeat": 3, "max": 3}]),
        macro([{"do": "walk", "repeat": MACRO_ACTIONS + 1}]),
        macro([{"do": "walk"}], {"type": "array"}),
        macro([{"do": "walk"}], {"type": "object", "properties": 3}),
    ]
    for spec in bad:
        with pytest.raises(CapabilityError):
            check_macro(spec, {"map_id"}, set(), BUILTIN)


class World:
    """A fake game: each walk moves one step; map 1 starts at x 5."""

    def __init__(self):
        self.x, self.done = 0, []

    def act(self, name, args):
        if args.get("direction") == "up":
            raise ValueError("blocked")
        self.x += 1
        self.done.append(name)
        return f"{name} ok"

    def observe(self):
        return {"x": self.x, "map_id": int(self.x >= 5)}


def test_macros_run_with_args_loops_and_nesting():
    world = World()
    walk_on = check_macro(
        macro(
            [
                {
                    "do": "walk",
                    "args": {"direction": "right"},
                    "until": {"obs": "map_id", "op": "==", "value": 1},
                    "max": 9,
                },
                {"do": "press", "args": {"button": "a"}, "repeat": 2},
                {"do": "walk", "while": {"obs": "x", "op": "<", "value": 3}, "max": 5},
            ],
            name="walk_on",
        ),
        {"x", "map_id"},
        set(),
        BUILTIN,
    )
    outer = check_macro(
        macro([{"do": "macro", "args": {"name": "walk_on", "args": {}}}], name="outer"), {"x"}, {"walk_on"}, BUILTIN
    )
    specs = {"walk_on": walk_on, "outer": outer}
    assert execute(outer, {}, world.act, world.observe, specs.__getitem__) == ["walk ok"] * 5 + ["press ok"] * 2
    with pytest.raises(CapabilityError, match="did not hold after 9 times"):
        world.x = -100
        execute(walk_on, {}, world.act, world.observe, specs.__getitem__)
    needs_n = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}
    counted = check_macro(macro([{"do": "walk"}], needs_n), set(), set(), BUILTIN)
    with pytest.raises(CapabilityError, match="arguments do not match"):
        execute(counted, {}, world.act, world.observe, None)
    with pytest.raises(ValueError, match="blocked"):  # a builtin action's error ends the call
        execute(
            check_macro(macro([{"do": "walk", "args": {"direction": "up"}}]), set(), set(), BUILTIN),
            {},
            world.act,
            world.observe,
            None,
        )


def test_a_macro_call_is_bounded():
    world = World()
    loop = macro([{"do": "walk", "repeat": MACRO_ACTIONS}, {"do": "walk"}], name="loop")
    with pytest.raises(CapabilityError, match=f"at most {MACRO_ACTIONS} actions"):
        execute(check_macro(loop, set(), set(), BUILTIN), {}, world.act, world.observe, None)
    assert len(world.done) == MACRO_ACTIONS
    deep = {
        f"m{i}": {
            "name": f"m{i}",
            "params": {"type": "object"},
            "steps": [{"do": "macro", "args": {"name": f"m{i + 1}"}}],
        }
        for i in range(6)
    }
    with pytest.raises(CapabilityError, match="nest at most 4"):
        execute(deep["m0"], {}, world.act, world.observe, deep.__getitem__)


def test_registry_versions_scopes_and_rollback(tmp_path):
    red = Memory(tmp_path / "memory.db", "run-1")
    red.open_run({"objective": "x", "rom_sha1": "red", "family": "pokemon-gen1"})
    registry = Registry(red)
    spec = check_observation(observation(decode="u16be"), BUILTIN)
    first = registry.define("observation", spec, "rom", 1)
    second = registry.define("observation", spec | {"description": "HP, big endian"}, "rom", 2)
    assert (first["version"], second["version"]) == (1, 2)
    assert [c["version"] for c in registry.active("observation")] == [2]
    with pytest.raises(sqlite3.DatabaseError, match="never changes"):
        red.write("UPDATE capabilities SET spec = '{}'")

    for error in (None, "boom", None, "boom"):
        assert registry.record(second, error, 2) is None
    note = registry.record(second, "boom", 2)
    assert note == "hp_low version 2 was rolled back (3 of its last 10 calls failed); version 1 is active again"
    assert registry.get("hp_low", "observation")["version"] == 1 and registry.usage(second) == (5, 3)

    blue = Memory(tmp_path / "memory.db", "run-2")  # another ROM of the family sees family scope only
    blue.open_run({"objective": "x", "rom_sha1": "blue", "family": "pokemon-gen1"})
    shared = Registry(blue)
    assert shared.active() == []
    registry.define("macro", check_macro(macro([{"do": "walk"}]), set(), set(), BUILTIN), "family", 3)
    assert [c["name"] for c in shared.active("macro")] == ["heal"]
    with pytest.raises(CapabilityError, match="no active observation"):
        shared.get("hp_low", "observation")


def test_params_refuse_references_and_missing_arguments_fail_cleanly():
    remote = {"type": "object", "$ref": "http://127.0.0.1:1/schema"}
    with pytest.raises(CapabilityError, match=r"\$ref"):
        check_macro(macro([{"do": "walk"}], remote), set(), set(), BUILTIN)
    optional = {"type": "object", "properties": {"button": {"type": "string"}}}
    spec = check_macro(
        macro([{"do": "press", "args": {"button": {"$arg": "button"}}}], optional), set(), set(), BUILTIN
    )
    with pytest.raises(CapabilityError, match="was not given"):
        execute(spec, {}, lambda *a: "ok", lambda: {}, lambda n: None)


def test_failures_count_across_runs_sharing_a_version(tmp_path):
    first = Memory(tmp_path / "memory.db", "run-1")
    first.open_run({"objective": "x", "rom_sha1": "red", "family": "pokemon-gen1"})
    cap = Registry(first).define("observation", check_observation(observation(decode="u8"), BUILTIN), "rom", 1)
    assert Registry(first).record(cap, "boom", 1) is None
    assert Registry(first).record(cap, "boom", 1) is None
    second = Memory(tmp_path / "memory.db", "run-2")
    second.open_run({"objective": "x", "rom_sha1": "red", "family": "pokemon-gen1"})
    later = Registry(second)
    assert "rolled back" in later.record(cap, "boom", 1)
    assert later.usage(cap) == (3, 3)
