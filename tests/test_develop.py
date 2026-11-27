from pathlib import Path

import pytest

from pokeagent import develop
from pokeagent.develop import Bench, denies, tokens
from pokeagent.memory import Memory


class Emu:
    def close(self):
        pass


class DevGame:
    """The development game: work RAM is a bytearray and a state is a copy of it."""

    def __init__(self, emu, adapter, **kw):
        self.emu, self.ram, self.kw = emu, bytearray(develop.WRAM[1]), kw

    def save_state(self):
        return bytes(self.ram)

    def load_state(self, state):
        self.ram[:] = state

    def restart(self, state):
        self.load_state(state)

    def peek(self, address, length):
        return bytes(self.ram[address - 0xC000 : address - 0xC000 + length])


class Live:
    adapter = registry = library = None
    tkv = Path("/run/live/tkv.json")

    def save_state(self):
        return bytes(develop.WRAM[1])

    def start(self):
        return Emu()


@pytest.fixture
def bench(tmp_path):
    return Bench(tmp_path / "run", {"tool": "step"}, Live(), DevGame, lambda: [])


def test_the_workspace_stays_inside_its_folder(bench, tmp_path, monkeypatch):
    assert bench.ws_write("tool.py", "def run(ctx):\n    pass\n") == "wrote tool.py (23 bytes)"
    bench.ws_write("notes/plan.md", "walk right")
    assert bench.ws_read("notes/plan.md") == "walk right"
    assert bench.ws_list() == "notes/plan.md (10 bytes)\ntool.py (23 bytes)\nfixtures: none"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("private")
    (bench.folder / "link").symlink_to(outside)
    (bench.folder / "file").symlink_to(outside / "secret")
    for path in ("/etc/passwd", "../x", "notes/../../x", "fixtures/a.state", "", "link/secret", "file"):
        with pytest.raises(ValueError):
            bench.ws_read(path)
        with pytest.raises(ValueError):
            bench.ws_write(path, "x")
    assert (outside / "secret").read_text() == "private"

    monkeypatch.setattr(develop, "WS_FILES", 3)  # the two files and the link to a file
    with pytest.raises(ValueError, match="at most 3 files"):
        bench.ws_write("third.py", "")
    bench.ws_write("tool.py", "pass\n")  # replacing a file is fine
    monkeypatch.setattr(develop, "WS_BYTES", 20)
    with pytest.raises(ValueError, match="at most"):
        bench.ws_write("tool.py", "x" * 11)  # with the 10-byte plan
    assert bench.ws_delete("notes/plan.md") == "deleted notes/plan.md"
    with pytest.raises(ValueError, match="no file"):
        bench.ws_read("notes/plan.md")


def test_fixtures_and_memory_tools_use_the_development_game(bench, monkeypatch):
    game = bench.game
    game.ram[5] = 7
    assert bench.fixture_save("start") == "saved fixture start"
    game.ram[5] = 9
    bench.fixture_load("start")
    assert game.ram[5] == 7 and list(bench.fixtures()) == ["start"]
    for bad in ("Start", "x", "../start"):
        with pytest.raises(ValueError, match="names are"):
            bench.fixture_save(bad)
    with pytest.raises(ValueError, match="no fixture gone; saved: start"):
        bench.fixture_load("gone")
    monkeypatch.setattr(develop, "MAX_FIXTURES", 1)
    with pytest.raises(ValueError, match="at most 1 fixtures"):
        bench.fixture_save("second")

    bench.snapshot("before")
    game.ram[0x10] = 0x42
    assert bench.diff("before") == "1 byte(s) differ\n0xc010: 0x00 -> 0x42"
    assert bench.search(0x4200, width=2) == "2 address(es): 0xc00f, 0xc010"  # either byte order
    assert bench.search(0x42, among=[0xC011]) == "0 address(es)"
    with pytest.raises(ValueError, match="no snapshot gone"):
        bench.diff("gone")
    with pytest.raises(ValueError, match="fit"):
        bench.search(256)

    assert bench.load() == "loaded the live state" and game.ram[0x10] == 0
    with pytest.raises(ValueError, match="not a checkpoint"):
        bench.load("1.state")


def test_validate_needs_the_requested_tools_files(bench, monkeypatch):
    with pytest.raises(ValueError, match=r"needs manifest\.json, tool\.py, test_tool\.py"):
        bench.validate("rom", 1, set(), set())
    bench.ws_write("manifest.json", '{"name": "other"}')
    bench.ws_write("tool.py", "")
    bench.ws_write("test_tool.py", "")
    with pytest.raises(ValueError, match='"name": "step"'):
        bench.validate("rom", 1, set(), set())
    bench.ws_write("manifest.json", '{"name": "step"}')
    trials = []
    monkeypatch.setattr(develop.validation, "validate", lambda trial, *args: trials.append(trial) or {"passed": False})
    bench.validate("rom", 1, set(), set())
    assert trials[0].kw["tkv"] == Live.tkv  # contract runs start from the live timeline store
    bench.reports = [{}] * 10
    with pytest.raises(ValueError, match="10 validations are used up"):
        bench.validate("rom", 1, set(), set())


def test_the_policy_allows_the_first_cycle_then_limits_attempts_and_token_share(tmp_path):
    memory = Memory(tmp_path / "memory.db", "run")
    memory.open_run({"objective": "x", "rom_sha1": "red", "family": "pokemon-gen1"})
    assert (
        tokens({"input_tokens": 1, "cache_creation_input_tokens": 2, "output_tokens": 3, "cache_read_input_tokens": 9})
        == 6
    )
    step, other = {"tool": "step"}, {"tool": "other"}
    play = {"input_tokens": 100, "output_tokens": 100, "cache_read_input_tokens": 10**6}
    memory.event("session_end", {"usage": play}, 1)
    memory.event("session_end", {"mode": "develop", "usage": {"input_tokens": 500}}, 2)
    assert denies(memory, step, 0.3, 2) is None  # no cycle yet: the first one is always allowed
    memory.event("development_cycle", {"tool": "step"}, 2)
    assert denies(memory, step, 0.9, 1) == "step already had 1 development session(s), the limit per tool"
    assert denies(memory, other, 0.3, 2) == "development has used 71% of this run's tokens; the limit is 30%"
    assert denies(memory, step, 0.9, 2) is None
