from pathlib import Path

import pytest
from PIL import Image, ImageChops

from pokeagent.emulator.client import Emulator, EmulatorError, RequestRefused
from pokeagent.emulator.cores import core_path

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def emu():
    with Emulator(core_path("sameboy"), FIXTURES / "dmg-acid2.gb", options={"sameboy_model": "Game Boy"}) as e:
        yield e


def test_info(emu):
    assert emu.info["library"] == "SameBoy"
    assert (emu.info["width"], emu.info["height"]) == (160, 144)


def test_acid2_matches_reference(emu):
    emu.run(frames=120)
    reference = Image.open(FIXTURES / "dmg-acid2-reference.png").convert("RGB")
    assert ImageChops.difference(emu.frame(), reference).getbbox() is None


def test_replay_from_state_is_deterministic(emu):
    script = [(["a"], 5), ([], 10), (["right", "b"], 25), ([], 30)] * 4

    def play() -> str:
        for buttons, frames in script:
            emu.run(buttons, frames)
        return emu.ram_hash()

    start = emu.save_state()
    first = play()
    after_first = emu.save_state()
    emu.load_state(start)
    assert play() == first
    assert emu.save_state() == after_first


def test_reads_ram_but_not_rom(emu):
    assert len(emu.read(0xC000, 0x2000)) == 0x2000
    assert len(emu.read(0xCFF0, 0x20)) == 0x20  # spans the two WRAM descriptors
    assert len(emu.read(0xFF80, 0x7F)) == 0x7F
    with pytest.raises(RequestRefused, match="not readable"):  # a tool error, not a dead worker
        emu.read(0x0100, 4)


def test_rejects_bad_requests(emu):
    with pytest.raises(RequestRefused, match="unknown buttons"):
        emu.run(["turbo"])
    with pytest.raises(RequestRefused, match="state size"):
        emu.load_state(b"\0" * 16)
    emu.run(frames=1)  # the worker is still healthy


def test_fresh_worker_saves_and_reloads_state():
    with Emulator(core_path("sameboy"), FIXTURES / "dmg-acid2.gb") as e:
        state = e.save_state()
    with Emulator(core_path("sameboy"), FIXTURES / "dmg-acid2.gb") as e:
        e.load_state(state)
        assert e.save_state() == state


def _tree_rss_mb(pid: int) -> int:
    """Resident memory of a process and all its descendants (the sandbox's bwrap processes and the worker)."""
    total, pending = 0, [pid]
    while pending:
        p = pending.pop()
        status = Path(f"/proc/{p}/status").read_text()
        total += int(next(line.split()[1] for line in status.splitlines() if line.startswith("VmRSS")))
        for task in Path(f"/proc/{p}/task").iterdir():
            pending += [int(c) for c in (task / "children").read_text().split()]
    return total // 1024


def test_worker_memory_stays_flat_while_running(emu):
    # SameBoy emits about 2.1 M audio samples per second; keeping them would grow the worker by ~8 MB a second.
    emu.run(frames=60)
    before = _tree_rss_mb(emu._proc.pid)
    for _ in range(15):
        emu.run(frames=60)
    assert _tree_rss_mb(emu._proc.pid) - before < 20


def test_dead_worker_raises(tmp_path):
    e = Emulator(core_path("sameboy"), FIXTURES / "dmg-acid2.gb")
    e.close()
    with pytest.raises(EmulatorError, match="exited"):
        e.run()


def test_stalled_worker_times_out_while_writing():
    import subprocess
    import time

    e = Emulator(core_path("sameboy"), FIXTURES / "dmg-acid2.gb")  # a short timeout could fail startup under load
    e.close()
    e.timeout = 0.5
    e._proc = subprocess.Popen(["sleep", "30"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, start_new_session=True)
    t = time.monotonic()
    with pytest.raises(EmulatorError, match="did not accept"):
        e.load_state(b"\0" * 1_000_000)  # larger than the pipe buffer; nothing reads it
    assert time.monotonic() - t < 5
    e.close()
