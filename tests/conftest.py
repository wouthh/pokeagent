import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from pokeagent.emulator.client import Emulator
from pokeagent.emulator.cores import core_path
from pokeagent.games.pokemon_gen1 import CORE_OPTIONS, ROMS, Gen1


def _roms_dir() -> Path | None:
    """The main checkout's ignored roms/ folder (worktrees find it through the git common dir)."""
    common = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=Path(__file__).parent,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return Path(common).parent / "roms" if common else None


def _find_rom() -> Path | None:
    """POKEAGENT_ROM, else the first supported ROM in the main checkout's ignored roms/ folder."""
    if env := os.environ.get("POKEAGENT_ROM"):
        return Path(env)
    for path in sorted((_roms_dir() or Path()).glob("*.gb")):
        if hashlib.sha1(path.read_bytes()).hexdigest() in ROMS:
            return path
    return None


@pytest.fixture(scope="session")
def find_rom():
    """find_rom(name): a ROM in the main checkout's roms/ folder by file name, skipping the test when it is missing."""

    def find(name: str) -> Path:
        path = (_roms_dir() or Path()) / name
        if not path.is_file():
            pytest.skip(f"no {name} in roms/")
        return path

    return find


def boot_new_game(gen1: Gen1, emu: Emulator, blind_intro: bool = False):
    """Start a new game to the bedroom and return the saved state. `blind_intro` presses through Yellow's long
    title and Oak speech first (its title screen shows the cursor tile, which would end the menu search early)."""
    emu.run([], 300)
    if blind_intro:
        for button, times in (("start", 12), ("a", 6)):
            for _ in range(times):
                gen1.press(emu, button, wait=60)
    else:
        for _ in range(60):  # title screen until the CONTINUE/NEW GAME menu
            if gen1.screen_mode(gen1.screen_tiles(emu), False)["cursor"]:
                break
            gen1.press(emu, "start", wait=40)
        gen1.press(emu, "a", wait=30)
    names = 0
    for _ in range(80):  # Oak's speech; take the first preset name (below NEW NAME) for player and rival
        outcome = gen1.advance_text(emu)["outcome"]
        state = gen1.state(emu)
        if outcome == "menu":
            x, y = state["screen"]["cursor_at"]
            assert gen1.choose(emu, gen1.screen_text(gen1.screen_tiles(emu))[y + 2][x + 1 :].split()[0])
            names += 1
        elif names == 2 and state["map_size"] != [0, 0]:
            break
    return emu.save_state()


@pytest.fixture(scope="session")
def rom_path() -> Path:
    path = _find_rom()
    if path is None:
        pytest.skip("no Pokémon Red/Blue ROM (set POKEAGENT_ROM or put one in roms/)")
    return path


@pytest.fixture(scope="session")
def game(rom_path):
    """(adapter, emulator, state of a new game standing in the bedroom). Boots once; tests load the state."""
    gen1 = Gen1(rom_path.read_bytes())
    with Emulator(core_path("sameboy"), rom_path, options=CORE_OPTIONS) as emu:
        yield gen1, emu, boot_new_game(gen1, emu)


@pytest.fixture(scope="session")
def yellow_game(find_rom):
    """(adapter, emulator, bedroom state) of a new Yellow game on the adapter's own core options."""
    rom = find_rom("Pokemon Yellow.gbc")
    gen1 = Gen1(rom.read_bytes())
    with Emulator(core_path("sameboy"), rom, options=gen1.CORE_OPTIONS) as emu:
        yield gen1, emu, boot_new_game(gen1, emu, blind_intro=True)
