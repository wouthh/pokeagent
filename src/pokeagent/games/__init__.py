"""Game adapters: game-specific state, walk grids, progress and macro actions.

`identify(rom)` picks the adapter for a ROM. An adapter is a class with
- constants: `ROMS` (sha1 -> title of the dumps it knows), `FAMILY`, `CORE` and `CORE_OPTIONS` (its libretro core),
  `DEFAULT_OBJECTIVE` and `DEFAULT_CHECK` (the trusted check of finishing the game), `OBSERVATIONS` (name -> reader
  of the state dict: what success checks may test), and the natural style's `STARTERS` (species index -> type ids),
  `TEAM_TYPES`, `SIDE_QUESTS` and `GLITCHES`;
- `recognises(rom) -> bool` (a classmethod) and a constructor taking the ROM bytes, which reads names from the ROM;
- `title`, `types` (type names by id), `species_name(index)`, `decode(bytes)` (the game's text encoding);
- `state(emu) -> dict` in the shared shape of `pokemon_gen1.Gen1.state`, `state_text(state, detail=False)`,
  `observations(state)`, `fingerprint(state)` and `progress_score(state, maps_visited, tiles_visited)`;
- controller-only macros `press`, `walk`, `walk_to`, `advance_text` and `grid(emu)` (with `render` and `path`), and
  `save_game(emu)` / `save_complete(sram)` for the in-game save.

`generic.Generic` follows the same contract for a cartridge no game adapter knows: its state holds only `in_game`
(False) and `frame_id`, which the runner reads as "nothing but screens to go on".
"""

import hashlib

from pokeagent.games.generic import Generic
from pokeagent.games.pokemon_gen1 import Gen1
from pokeagent.games.pokemon_gen2 import Gen2
from pokeagent.games.pokemon_gen3 import Gen3

ADAPTERS = (Gen1, Gen2, Gen3, Generic)  # Generic last: it takes any GB, GBC or GBA cartridge the others refuse


def identify(rom: bytes):
    """The adapter for `rom`, reading its names from it; ValueError for a ROM no adapter recognises."""
    for adapter in ADAPTERS:
        if adapter.recognises(rom):
            return adapter(rom)
    raise ValueError(f"unsupported ROM (sha1 {hashlib.sha1(rom).hexdigest()}): not a GB, GBC or GBA cartridge")
