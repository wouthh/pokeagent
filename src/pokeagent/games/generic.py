"""The adapter of last resort: any Game Boy, Game Boy Color or Game Boy Advance cartridge, known from its header alone.

It holds no game knowledge: no tables, no RAM layout, no map, party or text. The agent learns the game from
screenshots and from the read-only RAM tools. `state` is the screen's coarse identity, `press` waits for the picture to
settle, and the other controller macros refuse. Progress is novelty: screens not seen before on the timeline.
"""

import hashlib

from PIL import Image

from pokeagent.games import pokemon_gen1, pokemon_gen2, pokemon_gen3
from pokeagent.games.pokemon_gen3 import Gen3

LOGO = bytes.fromhex(  # the Nintendo logo every Game Boy cartridge header carries at 0x104
    "ceed6666cc0d000b03730083000c000d0008111f8889000edccc6ee6ddddd999bbbb67636e0eecccdddc999fbbb9333e"
)
PLATFORMS = {"gb": "Game Boy", "gbc": "Game Boy Color", "gba": "Game Boy Advance"}
NO_ADAPTER = "this game has no adapter for that; use press"
NO_SAVE = "this game has no adapter, so its in-game save format is unknown"
PROGRESS = "a screen not seen before on this timeline"
NOTE = (
    "No game state is read for this game (no adapter): only the screenshot, mem_read and your own observations show"
    " it. walk, walk_to and advance_text are refused; press works."
)
SHORT = "No game state is read for this game; the screenshot shows it."
ROMS: dict[str, str] = {}  # no dump is known by its hash
OBSERVATIONS = {"in_game": lambda s: False}  # nothing here tells a game is in progress
STARTERS: dict = {}  # no starters: the natural style's rules are Pokémon's
SIZE = (20, 16)  # a frame is judged on this many gray cells of 4 levels, so animation and noise do not count


def platform(rom: bytes) -> str | None:
    """ "gb", "gbc" or "gba" when the cartridge header is valid, else None."""
    if len(rom) >= 0x150 and rom[0x104:0x134] == LOGO:
        check = 0
        for b in rom[0x134:0x14D]:
            check = (check - b - 1) & 0xFF
        if check == rom[0x14D]:
            return "gbc" if rom[0x143] & 0x80 else "gb"
    if len(rom) >= 0xC0 and rom[0xB2] == 0x96 and (-(sum(rom[0xA0:0xBD]) + 0x19)) & 0xFF == rom[0xBD]:
        return "gba"
    return None


def frame_id(frame: Image.Image) -> str:
    small = frame.convert("L").resize(SIZE, Image.Resampling.BOX)
    return hashlib.sha1(bytes(v >> 6 for v in small.tobytes())).hexdigest()[:12]


class Generic:
    ROMS, OBSERVATIONS, STARTERS, PROGRESS, GLITCHES = ROMS, OBSERVATIONS, STARTERS, PROGRESS, ""
    DEFAULT_OBJECTIVE = "Play this game and get as far into it as you can."
    DEFAULT_CHECK = None
    BUTTONS = ("a", "b", "start", "select", "up", "down", "left", "right")
    _settled = Gen3._settled  # waits for the picture to stand still
    _picture = staticmethod(Gen3._picture)

    def press(self, emu, button: str, hold: int = 6, wait: int = 12) -> None:
        """Press once the screen has settled, then wait until it reacts and settles again, so the next press lands."""
        self._settled(emu)
        before = self._picture(emu)
        emu.run([button], hold)
        emu.run([], wait)
        for _ in range(wait, 30, 4):
            if self._picture(emu) != before:
                break
            emu.run([], 4)
        self._settled(emu)

    @classmethod
    def recognises(cls, rom: bytes) -> bool:
        return platform(rom) is not None

    def __init__(self, rom: bytes):
        kind = platform(rom)
        if kind is None:
            raise ValueError("not a GB, GBC or GBA cartridge (no valid header)")
        self.FAMILY = f"generic-{kind}"
        if kind == "gba":
            self.CORE, self.CORE_OPTIONS = pokemon_gen3.CORE, pokemon_gen3.CORE_OPTIONS
            self.BUTTONS = pokemon_gen3.BUTTONS
            self.RAM_RANGES, self.WORK_RAM = pokemon_gen3.RAM_RANGES, pokemon_gen3.WORK_RAM
            name = rom[0xA0:0xAC].split(b"\0")[0] + b" " + rom[0xAC:0xB0]
        else:
            self.CORE = pokemon_gen1.CORE
            self.CORE_OPTIONS = pokemon_gen2.CORE_OPTIONS if kind == "gbc" else pokemon_gen1.CORE_OPTIONS
            name = rom[0x134:0x144].split(b"\0")[0]
        name = "".join(c for c in name.decode("ascii", "replace") if c.isprintable()).strip() or "untitled"
        self.title = f"{name} ({PLATFORMS[kind]}; no adapter for this game)"

    @staticmethod
    def decode(data: bytes) -> str:
        raise ValueError("no text encoding is known for this game; decode as bytes")

    @staticmethod
    def state(emu) -> dict:
        return {"in_game": False, "frame_id": frame_id(emu.frame())}

    @staticmethod
    def state_text(state: dict, detail: bool = False) -> str:
        return NOTE if detail else SHORT

    @staticmethod
    def observations(state: dict) -> dict:
        return {name: read(state) for name, read in Generic.OBSERVATIONS.items()}

    @staticmethod
    def fingerprint(state: dict) -> dict:
        return {"screen": state["frame_id"]}

    @staticmethod
    def progress_score(state: dict, maps_visited: int, tiles_visited: int) -> int:
        return tiles_visited

    def walk(self, emu, direction, steps):
        raise ValueError(NO_ADAPTER)

    def walk_to(self, emu, x, y):
        raise ValueError(NO_ADAPTER)

    def advance_text(self, emu):
        raise ValueError(NO_ADAPTER)

    def save_game(self, emu) -> bool:
        raise ValueError(NO_SAVE)

    @staticmethod
    def save_complete(sram: bytes) -> bool:
        raise ValueError(NO_SAVE)
