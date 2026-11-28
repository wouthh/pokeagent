"""Game adapter for the English Pokémon Gold, Silver and Crystal (Game Boy Color), and hacks built on their engine.

RAM addresses come from the pret/pokegold and pret/pokecrystal disassemblies: Gold and Silver share one layout,
Crystal's is its own. Pokémon, item and move names, base data (types) and the collision permission table are found in
the user's ROM at runtime (`gen2_tables`), so no game text is stored in this repository and a hack that keeps the
engine works without an entry here. The base game is guessed from the header and checked against the game's own
invariants once a game runs. All game progress goes through controller input; RAM is only read.

Facts the code cannot say by itself (verified live on the three games):
- The game state lives in WRAM bank 1 (D000-DFFF). SameBoy's memory map points D000 at bank 1 whatever SVBK (FF70)
  selects (the game switches banks 2-7 in for a few frames, e.g. when a battle starts), so reads there never depend on
  it; banks 2-7 sit at 0x10000. Do not guard these reads on FF70: that made a running game read as no game.
- A tile of the tile map is a tile id, not a character: map tiles and Pokémon pictures decode as letters, so text is
  only read inside boxes (┌ ... ┘). The dialog box is rows 12-17 and its waiting arrow sits on the bottom border.
- Collision is stored per 16x16 step square (four bytes per block: top-left, top-right, bottom-left, bottom-right).
"""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from pokeagent.emulator.client import Emulator
from pokeagent.games.gen2_tables import find_tables
from pokeagent.games.pokemon_gen1 import (
    DIRECTIONS,
    FACING_CODES,
    OBSERVATIONS,
    STATUS,
    YELLOW_GLITCHES,
    Gen1,
    Grid,
    dvs,
)

ROMS = {
    "d8b8a3600a465308c9953dfa04f0081c05bdcb94": "Pokémon Gold (English)",
    "49b163f7e57702bc939d642a18f591de55d92dae": "Pokémon Silver (English)",
    "f2f52230b536214ef7c9924f483392993e226cfb": "Pokémon Crystal (English 1.1)",
}
FAMILY = "pokemon-gen2"
CORE = "sameboy"
# All three run on the colour hardware (Crystal needs it). The in-game clock runs on emulated time, not the host's.
CORE_OPTIONS = {"sameboy_model": "Game Boy Color", "sameboy_rtc": "accurate"}
HALL_OF_FAME = 0x1008  # map id = group << 8 | number: group 16, map 8
DEFAULT_OBJECTIVE = "Finish the game: defeat the Elite Four and the Champion and enter the Hall of Fame."
DEFAULT_CHECK = {"obs": "map_id", "op": "==", "value": HALL_OF_FAME}
# the starters (species number: type ids) and the type ids a team can draw (all but BIRD and the unused ids)
STARTERS = {152: (0x16,), 155: (0x14,), 158: (0x15,)}
TEAM_TYPES = (0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x07, 0x08, 0x09, 0x14, 0x15, 0x16, 0x17, 0x18, 0x19, 0x1A, 0x1B)
SIDE_QUESTS = (
    "Hatch a Pokémon from an Egg.",
    "Catch a Pokémon during the Bug-Catching Contest.",
    "Win a Pokémon as a prize at the Goldenrod Game Corner.",
    "Collect the Old Rod, the Good Rod and the Super Rod, and catch a Pokémon with each.",
    "Catch at least three different Unown in the Ruins of Alph.",
    "Make at least one of the trades that people in towns offer.",
    "Evolve a Pokémon with an evolution stone and another by trading or with high happiness.",
    "Catch one of the legendary Pokémon that roam Johto.",
    "Get the Bicycle and use it on a long route.",
    "Battle every trainer on the routes you cross.",
    "Catch a Pokémon of every type.",
    "Register five trainers' phone numbers in the Pokégear.",
    "Own a Pokémon you caught in the morning, one from the day and one from the night.",
    "Learn at least two HM moves and use them to reach a place you could not reach before.",
    "Visit Kanto and challenge its Gym Leaders after the Elite Four.",
    "Leave a Pokémon at the Day-Care for a while and collect it again.",
)
GLITCHES = YELLOW_GLITCHES  # generic: no particular glitch is named for Gen 2

CHARMAP = {0x50: "", 0x7F: " ", 0x74: " ", 0x75: "…", 0x4A: "PKMN", 0x54: "POKé", 0xE0: "'", 0xE1: "PK", 0xE2: "MN"}
CHARMAP |= {0xD0: "'d", 0xD1: "'l", 0xD2: "'m", 0xD3: "'r", 0xD4: "'s", 0xD5: "'t", 0xD6: "'v", 0xE3: "-", 0xE6: "?"}
CHARMAP |= {0xE7: "!", 0xE8: ".", 0xE9: "&", 0xEA: "é", 0xEC: "▷", 0xED: "▶", 0xEE: "▼", 0xEF: "♂", 0xF0: "¥"}
CHARMAP |= {0xF1: "×", 0xF2: ".", 0xF3: "/", 0xF4: ",", 0xF5: "♀"}  # noqa: RUF001 (the game's own symbols)
CHARMAP |= {0x79: "┌", 0x7A: "─", 0x7B: "┐", 0x7C: "│", 0x7D: "└", 0x7E: "┘", 0x9A: "(", 0x9B: ")", 0x9C: ":"}
CHARMAP |= {0x9D: ";", 0x9E: "[", 0x9F: "]"}
CHARMAP |= {0x80 + i: chr(ord("A") + i) for i in range(26)} | {0xA0 + i: chr(ord("a") + i) for i in range(26)}
CHARMAP |= {0xF6 + i: str(i) for i in range(10)}
CURSOR, WAIT_ARROW, BOX_TOP_LEFT, BOX_TOP_RIGHT, BOX_BOTTOM_LEFT, BOX_BOTTOM_RIGHT = 0xED, 0xEE, 0x79, 0x7B, 0x7D, 0x7E
BOX_SIDE, BOX_TOP = 0x7C, 0x7A

# WRAM / HRAM (pret pokegold and pokecrystal symbols). Bank-1 addresses are read through D000-DFFF.
RAM_LAYOUTS = {
    "gold-silver": {
        "TILEMAP": 0xC3A0,  # 20x18 screen tile ids
        "BLOCKS": 0xC700,  # the map's blocks with a 3-block border
        "MAP_GROUP": 0xDA00,  # group, number, y, x
        "MAP_HEIGHT": 0xD087,  # height, width in blocks
        "MAP_ATTRIBUTES": 0xD081,  # bank, ..., pointer at +3
        "TILESET": 0xD0C2,  # 12 bytes, see `Gen2.grid`
        "WARPS": 0xD945,  # count, pointer to the ROM list
        "SCRIPT_BANK": 0xD08C,
        "OBJECTS": 0xD1FD,  # 13 object structs of 0x28 bytes; the first is the player
        "FACING": 0xD205,
        "WALKING": 0xD204,
        "PLAYER_STATE": 0xD682,
        "PLAYER_NAME": 0xD1A3,
        "RIVAL_NAME": 0xD1B9,
        "PARTY_COUNT": 0xDA22,
        "PARTY_MONS": 0xDA2A,
        "PARTY_NICKS": 0xDB8C,
        "MONEY": 0xD573,
        "BADGES": 0xD57C,  # Johto, then Kanto
        "DEX_OWNED": 0xDBE4,
        "DEX_SEEN": 0xDC04,
        "NUM_ITEMS": 0xD5B7,
        "NUM_KEY_ITEMS": 0xD5E1,
        "NUM_BALLS": 0xD5FC,
        "TMHM": 0xD57E,
        "IS_IN_BATTLE": 0xD116,
        "ENEMY": 0xD0EF,
        "CUR_OT_MON": 0xCB41,
        "TIME_OF_DAY": 0xD157,
    },
    "crystal": {
        "TILEMAP": 0xC4A0,
        "BLOCKS": 0xC800,
        "MAP_GROUP": 0xDCB5,
        "MAP_HEIGHT": 0xD19E,
        "MAP_ATTRIBUTES": 0xD198,
        "TILESET": 0xD1D9,
        "WARPS": 0xDBFB,
        "SCRIPT_BANK": 0xD1A3,
        "OBJECTS": 0xD4D6,
        "FACING": 0xD4DE,
        "WALKING": 0xD4DD,
        "PLAYER_STATE": 0xD95D,
        "PLAYER_NAME": 0xD47D,
        "RIVAL_NAME": 0xD493,
        "PARTY_COUNT": 0xDCD7,
        "PARTY_MONS": 0xDCDF,
        "PARTY_NICKS": 0xDE41,
        "MONEY": 0xD84E,
        "BADGES": 0xD857,
        "DEX_OWNED": 0xDE99,
        "DEX_SEEN": 0xDEB9,
        "NUM_ITEMS": 0xD892,
        "NUM_KEY_ITEMS": 0xD8BC,
        "NUM_BALLS": 0xD8D7,
        "TMHM": 0xD859,
        "IS_IN_BATTLE": 0xD22D,
        "ENEMY": 0xD206,
        "CUR_OT_MON": 0xC663,
        "TIME_OF_DAY": 0xD269,
    },
}
SPECIES_COUNT, EGG, PARTY_MON_SIZE = 251, 0xFD, 0x30
# The in-game save in cartridge RAM bank 1 (pret sCheckValue1 .. sCheckValue2): SAVE_SIZE bytes, the first check value
# (99) just before the data, the data (`SAVE_MAIN`), its 16-bit little-endian sum, then the second check value (127).
SAVE_SIZE, CHECK_1, CHECK_2, NAME_AT = 0x8000, 99, 127, 0x200B
SAVE_START, SAVE_CHECK_1 = 0x2009, 0x2008
SAVE_LAYOUTS = {  # layout: end of the data (and the checksum's place for Gold and Silver), checksum, second check value
    "gold-silver": {"end": 0x2D69, "checksum": 0x2D69, "check_2": 0x2D6B},
    "crystal": {"end": 0x2B83, "checksum": 0x2D0D, "check_2": 0x2D0F},
}

# Ledges (hop tiles): from one, a step in these directions jumps two squares (the one in between is a wall).
HOPS = {
    0xA0: {"right"},
    0xA1: {"left"},
    0xA2: {"up"},
    0xA3: {"down"},
    0xA4: {"down", "right"},
    0xA5: {"down", "left"},
    0xA6: {"up", "right"},
    0xA7: {"up", "left"},
}
# Side walls (0xB0-0xB7) block entering them in a direction when the low three bits of the collision byte are in it.
SIDE_WALLS = {"down": {2, 6, 7}, "up": {3, 4, 5}, "right": {1, 5, 7}, "left": {0, 4, 6}}
TALL_GRASS, SURF_MOVE, FOG_BADGE = 0x18, 57, 3
PAGE_BLANKS = 150  # blank tiles that make a screen without boxes a text page (the overworld has almost none)
PERMISSION_WATER = 1
TIMES_OF_DAY = ("morning", "day", "night")


def layout_problems(read, layout: dict[str, int]) -> list[str]:
    """The RAM invariants a loaded game breaks under `layout` (none: it fits). Needs a game in progress: power-on RAM
    is noise and fails every layout. `read(addr, n) -> bytes`."""
    bad = []
    name = read(layout["PLAYER_NAME"], 11)
    end = name.find(0x50)
    if not (1 <= end <= 10 and all(0x7F <= b <= 0xF5 for b in name[:end])):
        bad.append("player name")
    count, species = read(layout["PARTY_COUNT"], 1)[0], read(layout["PARTY_COUNT"] + 1, 7)
    if count > 6 or species[count] != 0xFF or not all(1 <= b <= SPECIES_COUNT or b == EGG for b in species[:count]):
        bad.append("party list")
    items = read(layout["NUM_ITEMS"], 1)[0]
    if items > 20 or read(layout["NUM_ITEMS"] + 1 + 2 * items, 1)[0] != 0xFF:
        bad.append("bag list")
    group, number, height, width = read(layout["MAP_GROUP"], 2) + read(layout["MAP_HEIGHT"], 2)
    if not (1 <= group <= 26 and number >= 1 and 1 <= height <= 60 and 1 <= width <= 60):
        bad.append("map header")
    t = read(layout["TILESET"], 9)
    blocks, collision = int.from_bytes(t[4:6], "little"), int.from_bytes(t[7:9], "little")
    if not (0 < t[3] < 128 and 0x4000 <= blocks < 0x8000 and 0x4000 <= collision < 0x8000):
        bad.append("tileset pointers")
    return bad


@dataclass
class Gen2Grid(Grid):
    """Walkability in player steps. `tiles` holds each step square's collision byte (`passable` the bytes that may be
    walked on). Ledges and side walls are LAND in the game's permission table but are special: a hop tile is stood on
    like any floor, and a further step in its direction jumps two squares (over the wall that follows it); a side wall
    blocks entering it in some directions."""

    water: frozenset[int] = frozenset()  # collision bytes of water the player cannot cross (yet)

    def render(self, player: tuple[int, int], radius: int | None = None) -> str:
        """The shared ASCII map with `~` for water that needs Surf."""
        first, *rows = super().render(player, radius).split("\n")
        x0 = max(0, player[0] - radius) if radius else 0
        out = []
        for row in rows:
            y, cells = int(row[:3]), list(row[4:])
            out.append(
                row[:4]
                + "".join("~" if c == "#" and self.tiles[y][x0 + i] in self.water else c for i, c in enumerate(cells))
            )
        return "\n".join([first, *out])

    def walkable(self, x: int, y: int) -> bool:
        return super().walkable(x, y) or (self.inside(x, y) and (x, y) not in self.sprites and self.tiles[y][x] in HOPS)

    def moves(self, x: int, y: int):
        here = self.tiles[y][x]
        for direction, (dx, dy) in DIRECTIONS.items():
            nx, ny = x + dx, y + dy
            if not self.inside(nx, ny):
                continue
            there = self.tiles[ny][nx]
            if 0xB0 <= there <= 0xB7:
                open_ = there & 7 not in SIDE_WALLS[direction] and (nx, ny) not in self.sprites
            else:
                open_ = self.walkable(nx, ny)
            if open_:
                yield direction, (nx, ny)
            elif direction in HOPS.get(here, ()) and self.walkable(x + 2 * dx, y + 2 * dy):
                yield direction, (x + 2 * dx, y + 2 * dy)


class Gen2(Gen1):
    """Reads state and drives inputs for one English Gold, Silver or Crystal ROM (or a hack of their engine) inside an
    `Emulator`. The tables are scanned for; the base game comes from the header and the first state read of a running
    game checks the WRAM layout against the game's own invariants."""

    ROMS, FAMILY, CORE, CORE_OPTIONS = ROMS, FAMILY, CORE, CORE_OPTIONS
    DEFAULT_OBJECTIVE, DEFAULT_CHECK, OBSERVATIONS = DEFAULT_OBJECTIVE, DEFAULT_CHECK, OBSERVATIONS
    STARTERS, TEAM_TYPES, SIDE_QUESTS, GLITCHES = STARTERS, TEAM_TYPES, SIDE_QUESTS, GLITCHES
    CHARMAP, WAIT_ROW, STEP_FRAMES, REACT_FRAMES = CHARMAP, 17, 40, 64

    @classmethod
    def recognises(cls, rom: bytes) -> bool:
        return find_tables(rom) is not None

    def __init__(self, rom: bytes):
        tables = find_tables(rom)
        if tables is None:
            raise ValueError(f"unsupported ROM (sha1 {hashlib.sha1(rom).hexdigest()}): not an English Gen 2 game")
        header = rom[0x134:0x144].split(b"\0")[0].decode("ascii", "replace")
        self.known = hashlib.sha1(rom).hexdigest() in ROMS
        self.title = ROMS.get(hashlib.sha1(rom).hexdigest()) or f"Gen 2 ROM (header title {header})"
        self.rom = rom
        self.layout_checked = False  # set once a running game has confirmed (or replaced) the base game
        self._set_base(header.startswith("PM_CRYSTAL") or rom[0x143] == 0xC0)  # Crystal is colour-only
        self.species = [self.decode(self._rom(tables["species"], 2510)[i * 10 : i * 10 + 10]) for i in range(251)]
        self.items = self._names(tables["items"], 249)
        self.moves = self._names(tables["moves"], 251)
        bank, _ = tables["types"]
        pointers = self._rom(tables["types"], 2 * 0x1C)
        self.types = [self._names((bank, pointers[2 * i] | pointers[2 * i + 1] << 8), 1)[0] for i in range(0x1C)]
        self.base_data = tables["base_data"]
        self.permissions = self._rom(tables["permissions"], 256)
        self.landmarks = tables["landmarks"]
        names = [f"TM{n:02d}" for n in range(1, 51)] + [f"HM{n:02d}" for n in range(1, 8)]
        self.tm_items = [self.items.index(n) + 1 for n in names] if all(n in self.items for n in names) else []
        self._areas: dict = {}

    def _set_base(self, crystal: bool) -> None:
        """Select what depends on the base game: the RAM layout, the save layout and the map names."""
        self.crystal = crystal
        self.layout = "crystal" if crystal else "gold-silver"
        self.ram = SimpleNamespace(**RAM_LAYOUTS[self.layout])
        self.map_names = {}
        if self.known:
            name = "crystal" if crystal else "gold_silver"
            for line in Path(__file__).with_name(f"gen2_{name}_map_names.txt").read_text().splitlines():
                group, number, identifier = line.split()
                self.map_names[int(group) << 8 | int(number)] = identifier.replace("_", " ").title()

    def check_layout(self, emu: Emulator) -> None:
        """Once a game runs, confirm the WRAM layout the header suggested, or switch to the one that fits (hacks)."""
        if self.layout_checked:
            return
        fits = [name for name, layout in RAM_LAYOUTS.items() if not layout_problems(emu.read, layout)]
        if self.layout in fits or len(fits) == 1:
            self._set_base((self.layout if self.layout in fits else fits[0]) == "crystal")
            self.layout_checked = True

    # ---- names ----

    def species_name(self, index: int) -> str:
        return "EGG" if index == EGG else super().species_name(index)

    def item_name(self, index: int) -> str:
        return self.items[index - 1] if 1 <= index <= len(self.items) else f"item {index:#04x}"

    def map_name(self, emu: Emulator, map_id: int) -> str:
        """The map's name (known dumps) or its area name from the ROM's landmark table plus its raw ids (hacks)."""
        if name := self.map_names.get(map_id):
            return name
        area = self._area(emu.read(self.ram.MAP_ATTRIBUTES, 5))
        return f"{area + ' ' if area else ''}(map {map_id >> 8}:{map_id & 255})"

    def _area(self, attributes: bytes) -> str:
        """The landmark name of the map whose attribute pointer is (bank, address): the map's row in the ROM's map
        group tables is `bank, tileset, environment, pointer(2), landmark`."""
        bank, tileset, pointer = attributes[0], attributes[1], attributes[3:5]
        key = (bank, tileset, pointer)
        if key not in self._areas:
            pattern = re.compile(re.escape(bytes([bank])) + b".[\\x00-\\x07]" + re.escape(pointer), re.S)
            found = next((m for m in pattern.finditer(self.rom) if self.rom[m.start() + 1] == tileset), None)
            name = ""
            if found:
                landmark = self.rom[found.end()]
                where = self._offset(self.landmarks) + 4 * landmark
                pointer_at = int.from_bytes(self.rom[where + 2 : where + 4], "little")
                start = self._offset((self.landmarks[0], pointer_at))
                name = self.decode(self.rom[start : self.rom.index(0x50, start)]).strip()
            self._areas[key] = name
        return self._areas[key]

    # ---- state ----

    def screen_tiles(self, emu: Emulator) -> bytes:  # the tile map's address depends on the game
        return emu.read(self.ram.TILEMAP, 20 * 18)

    @classmethod
    def _inside(cls, tiles: bytes) -> bytearray:
        """1 for every tile inside a box (┌ ... ┘); picture boxes are skipped."""
        inside = bytearray(360)
        for at in (i for i, tile in enumerate(tiles) if tile == BOX_TOP_LEFT):
            x, y = at % 20, at // 20
            x2 = x + 1
            while x2 < 20 and tiles[y * 20 + x2] == BOX_TOP:
                x2 += 1
            if x2 >= 20 or tiles[y * 20 + x2] not in (BOX_TOP_RIGHT, BOX_TOP_LEFT):
                continue
            for y2 in range(y + 1, 18):
                if tiles[y2 * 20 + x] == BOX_BOTTOM_LEFT:
                    rows = [tiles[row * 20 + x + 1 : row * 20 + x2] for row in range(y + 1, y2)]
                    if sum(0x7F not in row for row in rows) >= 6:  # a Pokémon picture: no blanks in six rows
                        break
                    for row in range(y + 1, y2):
                        inside[row * 20 + x + 1 : row * 20 + x2] = b"\1" * (x2 - x - 1)
                    break
                if tiles[y2 * 20 + x] != BOX_SIDE:
                    break
        return inside

    @classmethod
    def screen_text(cls, tiles: bytes) -> list[str]:
        """The text of the screen, only from inside boxes: the other tiles are map, pictures and HUD tiles whose ids
        decode as letters. A page with no box and mostly blank tiles (naming screen, trainer card) is text all over:
        its rows count when they hold a letter or digit. Rows keep their columns, so a cursor's column locates its
        label."""
        inside = cls._inside(tiles)
        page = not any(inside) and tiles.count(0x7F) >= PAGE_BLANKS
        shown = bytes(t if page or inside[i] else 0x7F for i, t in enumerate(tiles))
        lines = [cls.decode(shown[r * 20 : (r + 1) * 20]).replace("·", " ").rstrip() for r in range(18)]
        return [line if not page or any(c.isalnum() for c in line) else "" for line in lines]

    @classmethod
    def screen_mode(cls, tiles: bytes, in_battle: bool) -> dict:
        """The shared modes, and "menu" for a text page (it takes input, and `advance_text` must not press A there)."""
        mode = super().screen_mode(tiles, in_battle)
        if mode["mode"] == "overworld" and not any(cls._inside(tiles)) and any(cls.screen_text(tiles)):
            mode["mode"] = "menu"
        return mode

    def state(self, emu: Emulator) -> dict:
        read = emu.read
        self.check_layout(emu)
        ram = self.ram
        group, number, y, x = read(ram.MAP_GROUP, 4)
        height, width = read(ram.MAP_HEIGHT, 2)
        map_id = group << 8 | number
        tiles = self.screen_tiles(emu)
        facing = FACING_CODES.get(read(ram.FACING, 1)[0] & 0x0C, "?")
        party = []
        mons, nicks = read(ram.PARTY_MONS, PARTY_MON_SIZE * 6), read(ram.PARTY_NICKS, 11 * 6)
        for i in range(min(read(ram.PARTY_COUNT, 1)[0], 6)):
            mon = mons[i * PARTY_MON_SIZE : (i + 1) * PARTY_MON_SIZE]
            status = mon[0x20]
            word = lambda at, mon=mon: int.from_bytes(mon[at : at + 2])  # noqa: E731
            party.append(
                {
                    "species": self.species_name(mon[0]),
                    "name": self.decode(nicks[i * 11 : i * 11 + 11]),
                    "level": mon[0x1F],
                    "hp": word(0x22),
                    "max_hp": word(0x24),
                    "status": "asleep" if status & 7 else STATUS.get(status & 0xF8, "ok" if not status else "status"),
                    "types": self.base_types(mon[0]),
                    "item": self.item_name(mon[1]) if mon[1] else "",
                    "moves": [self.move_name(mv) for mv in mon[2:6] if mv],
                    "pp": [(pp & 0x3F, pp >> 6) for mv, pp in zip(mon[2:6], mon[0x17:0x1B], strict=True) if mv],
                    "exp": int.from_bytes(mon[8:11]),
                    "stat_names": ("Atk", "Def", "Spd", "SpA", "SpD"),
                    "stats": [word(at) for at in (0x26, 0x28, 0x2A, 0x2C, 0x2E)],
                    # Atk, Def, Spd, Spc, then HP, as in Gen 1 (`stat_line` labels them)
                    "stat_exp": [word(at) for at in (13, 15, 17, 19, 11)],
                    "dvs": dvs(mon[0x15], mon[0x16]),
                }
            )
        player_name = self.decode(read(ram.PLAYER_NAME, 11))
        in_game = bool(group and height and width) and bool(player_name) and "·" not in player_name
        battle_kind = read(ram.IS_IN_BATTLE, 1)[0] if in_game else 0
        battle = None
        if battle_kind:
            e = read(ram.ENEMY, 0x14)
            battle = {
                "kind": {1: "wild", 2: "trainer"}.get(battle_kind, "lost"),
                "enemy": self.species_name(e[0]),
                "enemy_level": e[0xD],
                "enemy_hp": int.from_bytes(e[0x10:0x12]),
                "enemy_pos": read(ram.CUR_OT_MON, 1)[0],  # which party member of the enemy is out
                "enemy_max_hp": int.from_bytes(e[0x12:0x14]),
            }
        johto, kanto = read(ram.BADGES, 2)
        owned = read(ram.DEX_OWNED, 32)
        time = read(ram.TIME_OF_DAY, 1)[0]
        state = {
            "map_id": map_id,
            "map": self.map_name(emu, map_id) if in_game else f"map {group}:{number}",
            "x": x,
            "y": y,
            "facing": facing,
            "map_size": [width * 2, height * 2],
            "screen": self.screen_mode(tiles, bool(battle_kind)),
            "screen_text": [line for line in self.screen_text(tiles) if line.strip()],
            "player_name": player_name,
            "rival_name": self.decode(read(ram.RIVAL_NAME, 11)),
            "party": party,
            "bag": self._bag(read),
            "money": int.from_bytes(read(ram.MONEY, 3)),
            "badges": johto.bit_count() + kanto.bit_count(),
            "badge_bits": johto | kanto << 8,
            "badge_max": 16,
            "badge_note": f" (Johto {johto.bit_count()}, Kanto {kanto.bit_count()})",
            "dex_owned": (sum(b.bit_count() for b in owned[:31]) + (owned[31] & 7).bit_count()) if party else 0,
            "dex_seen": sum(b.bit_count() for b in read(ram.DEX_SEEN, 31))
            + (read(ram.DEX_SEEN + 31, 1)[0] & 7).bit_count(),
            "battle": battle,
            "in_game": in_game,
            "layout_unverified": in_game and not self.layout_checked,
        }
        if time < len(TIMES_OF_DAY):
            state["time_of_day"] = TIMES_OF_DAY[time]
        return state

    def base_types(self, species: int) -> list[str]:
        """The species' types from the ROM's base data (the party struct does not hold them)."""
        if not 1 <= species <= SPECIES_COUNT:
            return []
        first, second = self._rom((self.base_data[0], self.base_data[1] + (species - 1) * 32 + 7), 2)
        return list(dict.fromkeys(self.types[t] for t in (first, second) if t < len(self.types)))

    def _bag(self, read) -> list[dict]:
        """The four pockets as one list: items, key items (one each), balls and the TMs and HMs held."""
        ram, bag = self.ram, []
        raw = read(ram.NUM_ITEMS + 1, 40)
        for i in range(min(read(ram.NUM_ITEMS, 1)[0], 20)):
            bag.append({"item": self.item_name(raw[2 * i]), "qty": raw[2 * i + 1]})
        keys = read(ram.NUM_KEY_ITEMS + 1, 25)
        bag += [{"item": self.item_name(keys[i]), "qty": 1} for i in range(min(read(ram.NUM_KEY_ITEMS, 1)[0], 25))]
        raw = read(ram.NUM_BALLS + 1, 24)
        for i in range(min(read(ram.NUM_BALLS, 1)[0], 12)):
            bag.append({"item": self.item_name(raw[2 * i]), "qty": raw[2 * i + 1]})
        for item, count in zip(self.tm_items, read(ram.TMHM, 57), strict=False):
            if count:
                bag.append({"item": self.item_name(item), "qty": count})
        return bag

    # ---- walk grid ----

    def grid(self, emu: Emulator) -> Gen2Grid:
        ram = self.ram
        height, width = emu.read(ram.MAP_HEIGHT, 2)
        stride = width + 6
        blocks = emu.read(ram.BLOCKS, stride * (height + 6))
        t = emu.read(ram.TILESET, 9)  # tileset bank, pointer(2), blocks bank, pointer(2), collision bank, pointer(2)
        bank, pointer = t[6], int.from_bytes(t[7:9], "little")
        collision = [[0] * width * 2 for _ in range(height * 2)]
        for by in range(height):
            for bx in range(width):
                four = self._rom((bank, pointer + blocks[(by + 3) * stride + bx + 3] * 4), 4)
                for sy in range(2):
                    for sx in range(2):
                        collision[by * 2 + sy][bx * 2 + sx] = four[2 * sy + sx]
        perm, state = self.permissions, emu.read(ram.PLAYER_STATE, 1)[0]
        surf = state in (4, 8) or self._can_surf(emu)
        passable = frozenset(
            c for c in range(256) if not 0xA0 <= c <= 0xB7 and (perm[c] == 0 or (surf and perm[c] == PERMISSION_WATER))
        )
        structs = emu.read(ram.OBJECTS, 0x28 * 13)
        sprites = {
            (structs[i * 0x28 + 0x10] - 4, structs[i * 0x28 + 0x11] - 4) for i in range(1, 13) if structs[i * 0x28]
        }
        n = min(emu.read(ram.WARPS, 1)[0], 32)
        rows = self._rom((emu.read(ram.SCRIPT_BANK, 1)[0], int.from_bytes(emu.read(ram.WARPS + 1, 2), "little")), 5 * n)
        warps = {(rows[5 * i + 1], rows[5 * i]) for i in range(n)}
        ledges = {(next(iter(sorted(d))), -1, c): next(iter(sorted(d))) for c, d in HOPS.items()}
        water = frozenset(c for c in range(256) if perm[c] == PERMISSION_WATER and c not in passable)
        return Gen2Grid(
            width * 2, height * 2, collision, passable, frozenset(), ledges, sprites, warps, TALL_GRASS, water
        )

    def _can_surf(self, emu: Emulator) -> bool:
        """Surf is usable: the Fog Badge and a party member that knows the move."""
        if not emu.read(self.ram.BADGES, 1)[0] >> FOG_BADGE & 1:
            return False
        count = min(emu.read(self.ram.PARTY_COUNT, 1)[0], 6)
        mons = emu.read(self.ram.PARTY_MONS, PARTY_MON_SIZE * 6)
        return any(SURF_MOVE in mons[i * PARTY_MON_SIZE + 2 : i * PARTY_MON_SIZE + 6] for i in range(count))

    # ---- macro actions (controller input only) ----

    def press(self, emu: Emulator, button: str, hold: int = 4, wait: int = 12) -> None:
        """Menus here drop presses shorter than about four frames."""
        super().press(emu, button, hold, wait)

    def _position(self, emu: Emulator) -> tuple[int, int, int]:
        group, number, y, x = emu.read(self.ram.MAP_GROUP, 4)
        return group << 8 | number, x, y

    def _moving(self, emu: Emulator) -> bool:
        return emu.read(self.ram.WALKING, 1)[0] != 0xFF  # the player object's walking field is 0xFF when standing

    def _blocks(self, emu: Emulator) -> bytes:
        return emu.read(self.ram.BLOCKS, 64)

    def _settle(self, emu: Emulator, limit: int = 100, still: int = 12) -> None:
        """Run frames until the player has stood still for `still` frames: a warp starts ~10 frames after the step."""
        quiet = 0
        for _ in range(limit):
            quiet = 0 if self._moving(emu) else quiet + 1
            if quiet >= still:
                return
            emu.run([], 1)

    def _settle_map(self, emu: Emulator, old_blocks: bytes, limit: int = 200) -> None:
        """After a warp wait until the new map's blocks have loaded, then until the player (who may walk out of a
        door or stairs by themselves, ~20 frames after the map loads) has stood still for a while."""
        for _ in range(0, limit, 4):
            if self._blocks(emu) != old_blocks:
                break
            emu.run([], 4)
        self._settle(emu, still=30)

    def save_complete(self, sram: bytes) -> bool:
        """Whether cartridge RAM holds a whole in-game save: the right size, both check values, a player name and the
        game's 16-bit checksum of the data."""
        layout = SAVE_LAYOUTS[self.layout]
        if len(sram) != SAVE_SIZE or sram[SAVE_CHECK_1] != CHECK_1 or sram[layout["check_2"]] != CHECK_2:
            return False
        name = self.decode(sram[NAME_AT : NAME_AT + 11])
        if not name or "·" in name:
            return False
        stored = int.from_bytes(sram[layout["checksum"] : layout["checksum"] + 2], "little")
        return sum(sram[SAVE_START : layout["end"]]) & 0xFFFF == stored
