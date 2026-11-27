import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from pokeagent.emulator.client import Emulator
from pokeagent.emulator.cores import core_path
from pokeagent.games.pokemon_gen1 import CORE_OPTIONS, ROMS, Gen1


def _find_rom() -> Path | None:
    """POKEAGENT_ROM, else the first supported ROM in the main checkout's ignored roms/ folder."""
    if env := os.environ.get("POKEAGENT_ROM"):
        return Path(env)
    common = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=Path(__file__).parent,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if not common:
        return None
    for path in sorted(Path(common).parent.glob("roms/*.gb")):
        if hashlib.sha1(path.read_bytes()).hexdigest() in ROMS:
            return path
    return None


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
        emu.run([], 300)
        for _ in range(60):  # title screen until the CONTINUE/NEW GAME menu
            if gen1.screen_mode(gen1.screen_tiles(emu), False)["cursor"]:
                break
            gen1.press(emu, "start", wait=40)
        gen1.press(emu, "a", wait=30)
        names = 0
        for _ in range(40):  # Oak's speech; take the first preset name (below NEW NAME) for player and rival
            outcome = gen1.advance_text(emu)["outcome"]
            state = gen1.state(emu)
            if outcome == "menu":
                x, y = state["screen"]["cursor_at"]
                assert gen1.choose(emu, gen1.screen_text(gen1.screen_tiles(emu))[y + 2][x + 1 :].split()[0])
                names += 1
            elif names == 2 and state["map_size"] != [0, 0]:
                break
        yield gen1, emu, emu.save_state()
