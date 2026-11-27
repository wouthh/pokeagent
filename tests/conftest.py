import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from pokeagent.games.pokemon_gen1 import ROMS


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
