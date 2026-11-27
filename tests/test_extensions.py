import json
import tempfile
import textwrap
from pathlib import Path

import pytest

from pokeagent import extensions
from pokeagent.capabilities import CapabilityError, Registry, check_macro
from pokeagent.emulator.client import RequestRefused
from pokeagent.extensions import Budget, Refused, Runner, install
from pokeagent.memory import Memory
from pokeagent.validation import validate


class FakeAdapter:
    @staticmethod
    def state(game):
        return {"x": game.x, "money": game.money, "screen_text": ["HELLO"]}

    @staticmethod
    def fingerprint(state):
        return {"position": state["x"], "money": state["money"]}


class FakeGame:
    """A one-dimensional world: walk moves right, select spends money."""

    def __init__(self, tmp_path):
        memory = Memory(tmp_path / "memory.db", "run-1")
        memory.open_run({"objective": "x", "rom_sha1": "red", "family": "pokemon-gen1"})
        self.registry, self.library, self.tkv = Registry(memory), tmp_path / "library", tmp_path / "tkv.json"
        self.adapter, self.emu, self.x, self.money, self.frames = FakeAdapter(), self, 0, 100, 0

    def read(self, address, length):
        if address < 0x8000:
            raise RequestRefused(f"address {address:#06x} is not readable RAM")
        return bytes(range(length))

    def _observations(self):
        return {"x": self.x, "money": self.money}

    def _do(self, name, args):
        if name == "walk":
            self.x += args.get("steps", 1)
            return "walked"
        if name == "press" and args.get("button") == "select":
            self.money -= 10
            return "spent"
        if name == "press" and args.get("button") in ("a", "b"):
            return "pressed"
        raise ValueError(f"cannot {name} {args}")

    def run(self, buttons, frames):
        self.frames += frames

    def load_state(self, state):
        self.x, self.money = json.loads(state)

    def add(self, called, body, **manifest):
        source = "def run(ctx, **params):\n" + textwrap.indent(textwrap.dedent(body), "    ")
        manifest = {"name": called, "description": f"test tool {called}"} | manifest
        return install(self.registry, self.library, manifest, source, "rom", 1, {"x", "money"}, {"walk", "press"})


@pytest.fixture
def world(tmp_path):
    return FakeGame(tmp_path)


def test_a_tool_uses_ctx_within_its_contract(world):
    tool = world.add(
        "explorer",
        """
        before = ctx.observe("x")
        ctx.call("walk", steps=params["steps"])
        ctx.wait(30)
        ctx.log("walked")
        ctx.kv.set("trips", (ctx.kv.get("trips") or 0) + 1)
        ctx.tkv.set("last", ctx.state()["x"])
        try:
            ctx.read(0x0100)
        except Exception as e:
            refused = str(e)
        return {"from": before, "ram": ctx.read(0xC000, 2).hex(), "text": ctx.screen_text(), "refused": refused}
        """,
        params={"type": "object", "properties": {"steps": {"type": "integer"}}, "required": ["steps"]},
        examples=[{"steps": 1}],
        returns={"type": "object", "required": ["from"]},
        postconditions={"obs": "x", "op": ">", "value": {"before": "x"}},
        effects=["position"],
    )
    budget = Budget.start(world)
    result = Runner(world).call(tool, {"steps": 2}, budget)
    assert result == {"from": 0, "ram": "0001", "text": ["HELLO"], "refused": "address 0x0100 is not readable RAM"}
    assert world.x == 2 and world.frames == 30 and budget.logs == ["explorer: walked"]
    Runner(world).call(tool, {"steps": 1})
    assert world.registry.kv(tool) == {"trips": 2} and json.loads(world.tkv.read_text()) == {"explorer": {"last": 3}}


def test_calls_are_refused_before_they_run(world):
    tool = world.add(
        "careful",
        "ctx.call('walk')",
        params={"type": "object", "properties": {"n": {"type": "integer"}}},
        preconditions={"obs": "money", "op": ">=", "value": 50},
        effects=["position"],
    )
    with pytest.raises(Refused, match="do not match"):
        Runner(world).call(tool, {"n": "two"})
    world.money = 10
    with pytest.raises(Refused, match="preconditions do not hold"):
        Runner(world).call(tool, {})
    assert world.x == 0


@pytest.mark.parametrize(
    ("body", "manifest", "error"),
    [
        ("ctx.press('select')", {"effects": ["position"]}, "changed money, which its effects do not declare"),
        ("ctx.press('a')", {"postconditions": {"obs": "x", "op": ">", "value": {"before": "x"}}}, "postcondition"),
        ("return 'yes'", {"returns": {"type": "integer"}}, "does not match returns"),
        ("return 1 / 0", {}, "failed: ZeroDivisionError"),
        (
            "for _ in range(5): ctx.call('walk')",
            {"invariants": {"obs": "x", "op": "<=", "value": 3}, "effects": ["position"]},
            "invariant failed",
        ),
        ("for _ in range(700): ctx.press('a')", {}, "inputs is used up"),
        ("ctx.call('press', button='a', times=700)", {}, "inputs is used up"),
        ("for _ in range(31): ctx.wait(600)", {}, "frames or time is used up"),
        ("while True: pass", {}, "ran out of time"),
        ("x = bytearray(2 << 30)", {}, "failed: MemoryError"),
    ],
)
def test_contract_violations_and_budgets_fail_the_call(world, body, manifest, error):
    tool = world.add("broken", body, **manifest)
    budget = Budget.start(world)
    budget.deadline -= budget.seconds - 3  # three seconds are plenty for every case
    with pytest.raises(CapabilityError, match=error):
        Runner(world).call(tool, {}, budget)


def test_tools_call_each_other_within_one_budget(world):
    world.add("step", "ctx.call('walk')\nreturn ctx.observe('x')", effects=["position"])
    outer = world.add("twice", "return [ctx.call('step'), ctx.call('step')]", effects=["position"])
    assert Runner(world).call(outer, {}) == [1, 2]
    deep = world.add("deep", "return ctx.call('deep')")
    with pytest.raises(CapabilityError, match="at most 4 deep"):
        Runner(world).call(deep, {})
    catches = world.add("catches", "try:\n    ctx.call('nothing')\nexcept Exception as e:\n    return str(e)")
    assert Runner(world).call(catches, {}) == "no builtin action or active capability named 'nothing'"


@pytest.fixture
def home_secret():
    """A file in the real home cache (the sandbox's own /tmp hides pytest's tmp_path anyway)."""
    cache = Path.home() / ".cache"
    cache.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pokeagent-test-", dir=cache) as folder:
        secret = Path(folder) / "secret.txt"
        secret.write_text("secret")
        yield secret


def test_a_tool_cannot_reach_the_host(world, tmp_path, home_secret):
    (tmp_path / "secret.txt").write_text("secret")
    tool = world.add(
        "escape",
        f"""
        import os, socket, subprocess
        tried = {{}}
        for name, attempt in [
            ("home", lambda: open({str(home_secret)!r}).read()),
            ("tmp", lambda: open({str(tmp_path / "secret.txt")!r}).read()),
            ("database", lambda: open({str(tmp_path / "memory.db")!r}, 'rb').read(1)),
            ("network", lambda: socket.create_connection(("1.1.1.1", 443), timeout=2)),
            ("library", lambda: open("/ext/tool.py", "a").write("x")),
            ("spawn", lambda: [subprocess.Popen(["sleep", "5"]) for _ in range(40)]),
        ]:
            try:
                attempt()
                tried[name] = "worked"
            except Exception as e:
                tried[name] = type(e).__name__
        open("/tmp/scratch", "w").write("fine")
        tried["own_tmp"] = open("/tmp/scratch").read()
        return tried
        """,
    )
    tried = Runner(world).call(tool, {})
    assert tried.pop("own_tmp") == "fine" and all(outcome != "worked" for outcome in tried.values()), tried
    assert extensions.LIMITS["RLIMIT_NPROC"] < 40


def test_manifests_are_checked(world):
    bad = [
        ({"effects": ["teleport"]}, "effects must be"),
        ({"params": {"type": "array"}}, "object schema"),
        ({"returns": {"type": 3}}, "not a valid JSON schema"),
        ({"returns": {"$ref": "https://example.com/schema.json"}}, "may not use \\$ref"),
        ({"requires": ["ghost"]}, "do not exist"),
        ({"prefer_over": "fly"}, "prefer_over names a builtin"),
        ({"preconditions": {"obs": "x", "op": ">", "value": {"before": "x"}}}, "a value is a number"),
        ({"postconditions": {"obs": "x", "op": ">", "value": {"before": "hp"}}}, "unknown observations"),
        ({"compatibility": {"family": "zelda"}}, "family is pokemon-gen1"),
        ({"compatibility": {"roms": ["blue"]}}, "not in the tool's compatibility"),
        ({"api_version": 2}, "api_version must be 1"),
        ({"name": "walk"}, "builtin name"),
    ]
    for manifest, error in bad:
        with pytest.raises(CapabilityError, match=error):
            world.add("tool_x", "return 1", **manifest)
    tool = world.add("walker", "return 1", prefer_over="walk_to")
    assert world.registry.get("walker", "tool")["spec"]["prefer_over"] == "walk_to"
    assert (world.library / tool["spec"]["sha256"] / "tool.py").exists()


def state(x, money=100):
    return json.dumps([x, money]).encode()


STEP = "def run(ctx, steps=1):\n    ctx.call('walk', steps=steps)\n    return ctx.observe('x')\n"
TESTS = """
def test_walks_from_the_fixture(ctx):
    ctx.load_fixture("far")
    assert ctx.call("step", steps=2) == 12


def test_writes_kv(ctx):
    ctx.kv.set("seen", True)
"""
PASSING = {"tool.py": STEP, "test_tool.py": "def test_ok(ctx):\n    assert True\n"}
MANIFEST = {
    "name": "step",
    "description": "walk right",
    "params": {"type": "object", "properties": {"steps": {"type": "integer", "minimum": 1}}},
    "examples": [{}, {"steps": 3}],
    "effects": ["position"],
}


def check(world, manifest=None, files=None, states=None):
    return validate(
        world,
        manifest or MANIFEST,
        {"tool.py": STEP, "test_tool.py": TESTS} if files is None else files,
        {"far": state(10)},
        states or [("live", state(0))],
        "rom",
        1,
        {"x", "money"},
        {"walk", "press"},
    )


def test_a_tool_that_passes_is_staged_until_activation(world):
    report = check(world, states=[("live", state(0)), ("a", state(1)), ("b", state(2)), ("c", state(3))])
    assert report["passed"] and report["version"] == 1, report
    steps = [s["step"] for s in report["steps"]]
    assert steps == ["manifest", "ruff", "tests", "contract on live", "contract on a", "contract on b", "contract runs"]
    assert world.registry.active("tool") == [] and world.registry.kv({"scope": "rom:red", "name": "step"}) == {}
    assert world.registry.activate_staged(2) == ["step version 1 (tool) is now active"]
    assert world.registry.get("step", "tool")["version"] == 1 and world.registry.staged() == []
    assert world.registry.memory.events("validation")[-1]["data"]["passed"]


@pytest.mark.parametrize(
    ("change", "failed", "detail"),
    [
        ({"files": {"tool.py": STEP}}, "manifest", "needs tool.py and test_tool.py"),
        ({"files": {"tool.py": STEP + "\nundefined_name\n", "test_tool.py": TESTS}}, "ruff", "F821"),
        (
            {"files": {"tool.py": STEP, "test_tool.py": "def test_no(ctx):\n    assert ctx.call('step') == 9\n"}},
            "tests",
            "0 of 1 passed",
        ),
        ({"files": {"tool.py": STEP, "test_tool.py": "x = 1\n"}}, "tests", "no test_* function"),
        # the agent's own tests pass in both cases below; the trusted contract runs fail
        ({"manifest": MANIFEST | {"effects": []}, "files": PASSING}, "contract on live", "changed position"),
        (
            {"manifest": MANIFEST | {"preconditions": {"obs": "x", "op": ">=", "value": 5}}},
            "contract runs",
            "held in no state",
        ),
    ],
)
def test_validation_fails_on_any_step(world, change, failed, detail):
    report = check(world, **change)
    assert not report["passed"] and report["steps"][-1]["step"] == failed, report
    assert detail in report["steps"][-1]["detail"] and world.registry.staged() == []


def test_a_newer_validation_replaces_a_staged_version(world):
    check(world)
    check(world, manifest=MANIFEST | {"description": "walk right, v2"})
    assert [c["version"] for c in world.registry.staged()] == [2]
    world.registry.activate_staged()
    assert world.registry.get("step", "tool")["spec"]["description"] == "walk right, v2"


def test_a_macro_a_tool_calls_spends_the_tools_budget(world):
    spec = check_macro(
        {
            "name": "mash",
            "description": "press a many times",
            "steps": [{"do": "press", "args": {"button": "a", "times": 10}, "repeat": 100}],
        },
        set(),
        set(),
        {"press", "walk"},
    )
    world.registry.define("macro", spec, "rom", 1)
    tool = world.add("masher", "ctx.call('mash')")
    with pytest.raises(CapabilityError, match="inputs is used up"):
        Runner(world).call(tool, {}, Budget.start(world, inputs=500))


def test_a_tool_that_closes_its_pipe_and_sleeps_is_stopped(world):
    tool = world.add("sleeper", "import os, time\nos.close(1)\ntime.sleep(60)")
    budget = Budget.start(world)
    budget.deadline -= budget.seconds - 3
    with pytest.raises(CapabilityError, match="ran out of time"):
        Runner(world).call(tool, {}, budget)


def test_contract_runs_do_not_see_what_the_agents_tests_wrote(world):
    files = {
        "tool.py": "def run(ctx):\n    if not ctx.tkv.get('flag'):\n        raise ValueError('no flag')\n",
        "test_tool.py": "def test_sets(ctx):\n    ctx.tkv.set('flag', True)\n",
    }
    report = check(world, MANIFEST | {"effects": [], "examples": [{}]}, files)
    assert not report["passed"] and "no flag" in report["steps"][-1]["detail"], report


def test_an_old_capability_table_is_widened_once(tmp_path):
    memory = Memory(tmp_path / "memory.db", "run-1")
    memory.open_run({"objective": "x", "rom_sha1": "red", "family": "pokemon-gen1"})
    memory.db.executescript(
        "CREATE TABLE capabilities (id INTEGER PRIMARY KEY, name TEXT NOT NULL, version INTEGER NOT NULL,"
        " kind TEXT NOT NULL CHECK (kind IN ('observation', 'macro')), scope TEXT NOT NULL, spec TEXT NOT NULL,"
        " status TEXT NOT NULL CHECK (status IN ('active', 'previous', 'retired')), run TEXT NOT NULL,"
        " session INTEGER, created REAL NOT NULL, UNIQUE (name, scope, version));"
        " INSERT INTO capabilities VALUES (1, 'old', 1, 'macro', 'rom:red', '{}', 'active', 'r', NULL, 0);"
    )
    registry = Registry(memory)
    assert [c["name"] for c in registry.active()] == ["old"]
    registry.define("tool", {"name": "t"}, "rom", 1, staged=True)
    assert [c["name"] for c in registry.staged()] == ["t"]


def test_a_family_tool_restricted_to_other_roms_is_refused_here(world):
    tool = world.add("red_only", "return 1", compatibility={"family": "pokemon-gen1", "roms": ["red"]})
    assert Runner(world).call(tool, {}) == 1
    world.registry.memory.scopes["rom"] = "rom:blue"
    with pytest.raises(Refused, match="this ROM is not in the tool's compatibility list"):
        Runner(world).call(tool, {})


def test_a_failed_nested_call_cannot_hide_a_broken_invariant(world):
    world.add("child", "ctx.press('select')\nreturn 'x'", effects=["money"], returns={"type": "integer"})
    parent = world.add(
        "parent",
        "try:\n    ctx.call('child')\nexcept Exception:\n    return 'caught'",
        effects=["money"],
        invariants={"obs": "money", "op": ">=", "value": 100},
    )
    with pytest.raises(CapabilityError, match="invariant failed"):
        Runner(world).call(parent, {})
