"""Game adapter for the English Pokémon Ruby, Sapphire, Emerald, FireRed and LeafGreen (Game Boy Advance).

RAM addresses come from the pret disassemblies (pokeruby, pokeemerald, pokefirered), by family: `rs` (Ruby and
Sapphire), `e` (Emerald) and `frlg` (FireRed and LeafGreen). Names and the other tables are found in the user's ROM at
runtime (`gen3_tables`), so no game text is stored here and a dump that differs from the pret builds still works.
The few ROM function addresses in `FUNCTIONS` (to tell menus and yes/no prompts apart) are tied to the exact ROMs
they were read from; any other ROM falls back to RAM-only signals. All game progress goes through controller
input; RAM is only read.
"""

import hashlib
import struct
from collections import deque
from dataclasses import dataclass, field
from types import SimpleNamespace

from pokeagent.emulator.client import Emulator
from pokeagent.games import gen3_tables as tables

ROMS = {
    "610b96a9c9a7d03d2bafb655e7560ccff1a6d894": "Pokémon Ruby (English, rev 1)",
    "79bbb886662008ed71fc2254a1bc44e0e8a634cd": "Pokémon Sapphire (English, rev 2 header)",
    "f3ae088181bf583e55daf962a92bb46f4f1d07b7": "Pokémon Emerald (English)",
    "dd5945db9b930750cb39d00c84da8571feebf417": "Pokémon FireRed (English, 1.1)",
    "7862c67bdecbe21d1d69ce082ce34327e1c6ed5e": "Pokémon LeafGreen (English, 1.1)",
}
FAMILY = "pokemon-gen3"
CORE = "mgba"
CORE_OPTIONS: dict[str, str] = {}  # mGBA's defaults work: its built-in BIOS boots every game, nothing else is needed
BUTTONS = ("a", "b", "start", "select", "up", "down", "left", "right", "l", "r")
# the memory the agent may read: EWRAM and IWRAM (never ROM, BIOS, video memory or I/O); the develop tools' work RAM
RAM_RANGES = ((0x02000000, 0x40000), (0x03000000, 0x8000))
WORK_RAM = (0x02000000, 0x40000)
DEFAULT_OBJECTIVE = "Finish the game: defeat the Elite Four and the Champion and enter the Hall of Fame."
DEFAULT_CHECK = {"obs": "hall_of_fame", "op": "==", "value": True}
OBSERVATIONS = {  # the values success checks may test, read from the adapter's state
    "map_id": lambda s: s["map_id"],
    "x": lambda s: s["x"],
    "y": lambda s: s["y"],
    "badges": lambda s: s["badges"],
    "party_count": lambda s: len(s["party"]),
    "max_level": lambda s: max((p["level"] for p in s["party"]), default=0),
    "dex_owned": lambda s: s["dex_owned"],
    "money": lambda s: s["money"],
    "in_battle": lambda s: s["battle"] is not None,
    "hall_of_fame": lambda s: s["hall_of_fame"],
    "in_game": lambda s: s["in_game"],
}
# type ids: 0 NORMAL ... 9 ??? ... 17 DARK; every real type is fair game for a team
TEAM_TYPES = tuple(t for t in range(18) if t != 9)
HOENN_STARTERS = {277: (12,), 280: (10,), 283: (11,)}  # species id: type ids (grass, fire, water)
KANTO_STARTERS = {1: (12, 3), 4: (10,), 7: (11,)}
HOENN_QUESTS = (
    "Make a Pokéblock from berries you grew and feed it to a Pokémon.",
    "Enter a Pokémon Contest and win a ribbon.",
    "Hatch an egg from the Day Care and raise the baby.",
    "Catch a Pokémon of every type you can find on the route you are on.",
    "Trade for a Pokémon with an NPC and raise it.",
    "Plant a berry and come back to harvest it.",
    "Visit every town you can reach before moving on to the next gym.",
    "Find a secret base and set up one of your own.",
    "Battle every trainer on the routes you cross.",
    "Catch a Pokémon at every fishing spot, using each rod you own.",
)
KANTO_QUESTS = (
    "Buy the Magikarp the salesman offers and raise it into a Gyarados.",
    "Get the Coin Case in Celadon City and win a prize at the Game Corner.",
    "Make at least two of the trades that people in towns offer.",
    "Collect the Old Rod, the Good Rod and the Super Rod, and catch a Pokémon with each.",
    "Revive a fossil at the Cinnabar Island lab.",
    "Receive the gift Eevee in Celadon Mansion and evolve it.",
    "Receive the gift Lapras at Silph Co.",
    "Win Hitmonlee or Hitmonchan at the fighting dojo in Saffron City.",
    "Show Professor Oak's aides enough caught species to receive the rewards he offers.",
    "Battle every trainer on the routes you cross.",
)
GLITCHES = ""  # no glitch is documented well enough to name here

# ---- RAM layouts, by family (pret symbols) ----
EWRAM = range(0x02000000, 0x02040000)
LAYOUTS = {
    "rs": SimpleNamespace(
        MAIN=0x03001770,
        IN_BATTLE=0x43D,
        SB1=0x02025734,
        SB2=0x02024EA4,
        SB_POINTERS=False,
        PARTY=0x03004360,
        PARTY_COUNT=0x03004350,
        ENEMY=0x030045C0,
        BATTLE_FLAGS=0x020239F8,
        BATTLE_MONS=0x02024A80,
        BATTLE_PARTY_INDEX=0x02024A6A,
        CONTROLLERS=0x03004330,
        STRING4=(0x020234CC, 0x100),
        BATTLE_TEXT=(0x020238CC, 0x100),
        PRINTERS=None,
        FIELD_WINDOW=0x0202E87C,
        MESSAGE_MODE=0x030005A8,
        SCRIPT=0x030005B8,
        TASKS=0x03004B20,
        OBJECTS=0x030048A0,
        AVATAR=0x0202E858,
        MAP_HEADER=0x0202E828,
        MAP_LAYOUT=0x03004870,
        START_CURSOR=0x0202E8FC,
        MONEY=0x490,
        POCKETS=(
            (0x560, 20, "Items"),
            (0x5B0, 20, "Key items"),
            (0x600, 16, "Poké Balls"),
            (0x640, 64, "TMs"),
            (0x740, 46, "Berries"),
        ),
        FLAGS=0x1220,
        KEY=None,
        RIVAL=None,
        BADGES=0x807,
        POKEMON_GET=0x800,
        POKEDEX_GET=0x801,
        POKENAV_GET=0x802,
        GAME_CLEAR=0x804,
        PLAYER_NAME_SIZE=8,
    ),
    "e": SimpleNamespace(
        MAIN=0x030022C0,
        IN_BATTLE=0x439,
        SB1=0x03005D8C,
        SB2=0x03005D90,
        SB_POINTERS=True,
        PARTY=0x020244EC,
        PARTY_COUNT=0x020244E9,
        ENEMY=0x02024744,
        BATTLE_FLAGS=0x02022FEC,
        BATTLE_MONS=0x02024084,
        BATTLE_PARTY_INDEX=0x0202406E,
        CONTROLLERS=0x03005D60,
        STRING4=(0x02021FC4, 0x3E8),
        BATTLE_TEXT=(0x02022E2C, 0x12C),
        PRINTERS=0x020201B0,
        FIELD_WINDOW=None,
        MESSAGE_MODE=None,
        SCRIPT=0x03000E40,
        TASKS=0x03005E00,
        OBJECTS=0x02037350,
        AVATAR=0x02037590,
        MAP_HEADER=0x02037318,
        MAP_LAYOUT=0x03005DC0,
        START_CURSOR=0x0203760E,
        MONEY=0x490,
        POCKETS=(
            (0x560, 30, "Items"),
            (0x5D8, 30, "Key items"),
            (0x650, 16, "Poké Balls"),
            (0x690, 64, "TMs"),
            (0x790, 46, "Berries"),
        ),
        FLAGS=0x1270,
        KEY=0xAC,
        RIVAL=None,
        BADGES=0x867,
        POKEMON_GET=0x860,
        POKEDEX_GET=0x861,
        POKENAV_GET=0x862,
        GAME_CLEAR=0x864,
        PLAYER_NAME_SIZE=8,
    ),
    "frlg": SimpleNamespace(
        MAIN=0x030030F0,
        IN_BATTLE=0x439,
        SB1=0x03005008,
        SB2=0x0300500C,
        SB_POINTERS=True,
        PARTY=0x02024284,
        PARTY_COUNT=0x02024029,
        ENEMY=0x0202402C,
        BATTLE_FLAGS=0x02022B4C,
        BATTLE_MONS=0x02023BE4,
        BATTLE_PARTY_INDEX=0x02023BCE,
        CONTROLLERS=0x03004FE0,
        STRING4=(0x02021D18, 0x3E8),
        BATTLE_TEXT=(0x0202298C, 0x12C),
        PRINTERS=0x02020034,
        FIELD_WINDOW=None,
        MESSAGE_MODE=None,
        SCRIPT=0x03000EB0,
        TASKS=0x03005090,
        OBJECTS=0x02036E38,
        AVATAR=0x02037078,
        MAP_HEADER=0x02036DFC,
        MAP_LAYOUT=0x03005040,
        START_CURSOR=0x020370F4,
        MONEY=0x290,
        POCKETS=(
            (0x310, 42, "Items"),
            (0x3B8, 30, "Key items"),
            (0x430, 13, "Poké Balls"),
            (0x464, 58, "TMs"),
            (0x54C, 43, "Berries"),
        ),
        FLAGS=0xEE0,
        KEY=0xF20,
        RIVAL=0x3A4C,
        BADGES=0x820,
        POKEMON_GET=0x828,
        POKEDEX_GET=0x829,
        POKENAV_GET=None,
        GAME_CLEAR=0x82C,
        PLAYER_NAME_SIZE=8,
    ),
}
# the save blocks' sizes (their last chunk sets the size of a flash sector's checksummed data), by family
SAVE_BLOCKS = {"rs": (0x890, 0x3AC0), "e": (0xF2C, 0x3D88), "frlg": (0xF24, 0x3D68)}
STORAGE_SIZE = 0x83D0
SECTOR, SECTOR_DATA, SECTOR_SIGNATURE, SAVE_SIZE = 0x1000, 3968, 0x08012025, 0x20000

# ROM function pointers (Thumb, bit 0 set) of the exact pret builds, from their symbol files: what `gMain.callback2`
# (overworld, battle), `gBattlerControllerFuncs[0]` (the action and move menus), the script engine's native wait
# (the message is done, waiting for A or B) and the tasks of yes/no prompts and menus point at. Another ROM has none.
FUNCTIONS = {
    "610b96a9c9a7d03d2bafb655e7560ccff1a6d894": dict(
        overworld=0x80543C5,
        battle=0x800F809,
        wait=0x80670E1,
        action=(0x802C099,),
        move=(0x802C68D,),
        yes_no=(0x80B5505,),
        menu=(0x80B52D5, 0x80B56A5, 0x8071275, 0x80712D5, 0x80716DD),
    ),
    "f3ae088181bf583e55daf962a92bb46f4f1d07b7": dict(
        overworld=0x8085E5D,
        battle=0x8038421,
        wait=0x809AC99,
        action=(0x8057589, 0x81593D9),
        move=(0x8057BFD,),
        yes_no=(0x80E215D,),
        menu=(0x80E2059, 0x80E2309, 0x809F9B5, 0x809FA35, 0x80DFB89, 0x80C7269),
    ),
    "dd5945db9b930750cb39d00c84da8571feebf417": dict(
        overworld=0x80565C9,
        battle=0x8011115,
        wait=0x806B8AD,
        action=(0x802E44D, 0x80DD5B9, 0x80E7651),
        move=(0x802EA25, 0x80E799D),
        yes_no=(0x809CE69,),
        menu=(0x80CBB3D, 0x809CCAD, 0x806F205, 0x809AC25, 0x808C3B1),
    ),
    "7862c67bdecbe21d1d69ce082ce34327e1c6ed5e": dict(
        overworld=0x80565C9,
        battle=0x8011115,
        wait=0x806B8AD,
        action=(0x802E44D, 0x80DD591, 0x80E7629),
        move=(0x802EA25, 0x80E7975),
        yes_no=(0x809CE3D,),
        menu=(0x80CBB11, 0x809CC81, 0x806F205, 0x809ABF9, 0x808C385),
    ),
    # the Sapphire dump (matches no pret build): the same code found by its opening bytes, `overworld` also read live
    "79bbb886662008ed71fc2254a1bc44e0e8a634cd": dict(
        overworld=0x80543C9,
        battle=0x800F809,
        wait=0x80670E5,
        action=(0x802C099,),
        move=(0x802C68D,),
        yes_no=(0x80B54F1,),
        menu=(0x80B52C1, 0x8071259, 0x80712B9, 0x80716C1),
    ),
}

STATUS = {0x08: "poisoned", 0x10: "burned", 0x20: "frozen", 0x40: "paralyzed", 0x80: "badly poisoned"}
FACING = {1: "down", 2: "up", 3: "left", 4: "right"}
DIRECTIONS = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}
SUBSTRUCTS = (  # the order of a Pokémon's four data blocks (G growth, A attacks, E EVs, M misc), by personality % 24
    "GAEM",
    "GAME",
    "GEAM",
    "GEMA",
    "GMAE",
    "GMEA",
    "AGEM",
    "AGME",
    "AEGM",
    "AEMG",
    "AMGE",
    "AMEG",
    "EGAM",
    "EGMA",
    "EAGM",
    "EAMG",
    "EMGA",
    "EMAG",
    "MGAE",
    "MGEA",
    "MAGE",
    "MAEG",
    "MEGA",
    "MEAG",
)
STAT_NAMES = ("Atk", "Def", "Spe", "SpA", "SpD")
EV_NAMES = ("HP", "Atk", "Def", "Spe", "SpA", "SpD")
NATURE_STATS = ("Atk", "Def", "Spe", "SpA", "SpD")
# metatile behaviours (the same numbers in all three families unless noted)
GRASS = (0x02, 0x03)
WATER = (0x10, 0x11, 0x12, 0x14, 0x15)  # pond, deep, ocean...: walkable only while surfing
WATERFALL = 0x13
JUMPS = {0x38: "right", 0x39: "left", 0x3A: "up", 0x3B: "down"}  # ledges: one way, two tiles
LEDGE_MARKS = {"down": "v", "left": "<", "right": ">", "up": "^"}
REACT_FRAMES = 12


def decode(data: bytes) -> str:
    return tables.decode(data)


def valid_name(data: bytes) -> bool:
    """A player or Pokémon name: 1-7 or more printable characters, then the 0xFF terminator."""
    end = data.find(0xFF)
    return end >= 1 and all(b in tables.CHARMAP and b != 0 for b in data[:end])


def decode_money(raw: int, key: int) -> int:
    return raw ^ key


def decode_quantity(raw: int, key: int) -> int:
    return raw ^ (key & 0xFFFF)


def flag(flags: bytes, number: int, base: int) -> bool:
    """Whether event flag `number` is set in `flags` (the flag array starts at `base`)."""
    index = number - base
    return 0 <= index // 8 < len(flags) and bool(flags[index // 8] >> (index % 8) & 1)


def decode_mon(raw: bytes) -> dict | None:
    """One `struct Pokemon` (80 bytes, or 100 with its battle stats), or None if its checksum fails.

    The 48 data bytes are four 12-byte substructures in an order set by personality % 24, all XORed with
    personality ^ trainer id; the checksum is the 16-bit sum of the decrypted words.
    """
    if len(raw) < 80:
        return None
    pid, oid = struct.unpack_from("<II", raw, 0)
    data = bytearray(raw[0x20:0x50])
    for i in range(0, 48, 4):
        struct.pack_into("<I", data, i, struct.unpack_from("<I", data, i)[0] ^ pid ^ oid)
    if sum(struct.unpack("<24H", bytes(data))) & 0xFFFF != struct.unpack_from("<H", raw, 0x1C)[0]:
        return None
    order = SUBSTRUCTS[pid % 24]
    part = {c: bytes(data[order.index(c) * 12 : order.index(c) * 12 + 12]) for c in "GAEM"}
    species, item, exp, bonuses, friendship = struct.unpack_from("<HHIBB", part["G"])
    word = struct.unpack_from("<I", part["M"], 4)[0]
    mon = {
        "personality": pid,
        "nickname": raw[8:18],
        "egg": bool(raw[0x13] & 4),
        "species": species,
        "item": item,
        "exp": exp,
        "pp_bonuses": bonuses,
        "friendship": friendship,
        "moves": list(struct.unpack_from("<4H", part["A"])),
        "pp": list(part["A"][8:12]),
        "evs": list(part["E"][:6]),
        "ivs": [word >> (5 * i) & 31 for i in range(6)],
        "ability_slot": word >> 31,
    }
    if len(raw) >= 100:
        status, level, _, hp, max_hp, *stats = struct.unpack_from("<IBBHHHHHHH", raw, 0x50)
        mon |= {"status": status, "level": level, "hp": hp, "max_hp": max_hp, "stats": stats}
    return mon


def nature_effect(mon: dict) -> str:
    return f"+{mon['nature_up']} -{mon['nature_down']}" if mon["nature_up"] else "neutral"


def stat_line(names: tuple[str, ...], values: list[int]) -> str:
    return " ".join(f"{name} {value}" for name, value in zip(names, values, strict=False))


@dataclass
class Grid:
    """Walkability of the current map in player steps, from the game's own map grid (a 7-tile border of the
    neighbouring maps included, so a path can cross a map edge) and the metatile behaviours of its tilesets."""

    width: int
    height: int
    tiles: list[int]  # the map grid's u16 values, (height + 14) rows of (width + 15) columns
    behaviours: list[int]
    surfing: bool = False
    sprites: set[tuple[int, int]] = field(default_factory=set)
    warps: set[tuple[int, int]] = field(default_factory=set)

    @property
    def stride(self) -> int:
        return self.width + 15

    def _at(self, x: int, y: int) -> int | None:
        if not (-7 <= x < self.width + 8 and -7 <= y < self.height + 7):
            return None
        return (y + 7) * self.stride + x + 7

    def behaviour(self, x: int, y: int) -> int:
        i = self._at(x, y)
        return self.behaviours[i] if i is not None else 0

    def walkable(self, x: int, y: int) -> bool:
        i = self._at(x, y)
        if i is None or (x, y) in self.sprites:
            return False
        if (x, y) in self.warps:
            return True
        b = self.behaviours[i]
        if b in WATER:
            return self.surfing
        if b == WATERFALL:
            return False
        return not self.tiles[i] >> 10 & 3

    def moves(self, x: int, y: int):
        """Yield (direction, destination) for every legal single move from (x, y), ledge jumps included."""
        for direction, (dx, dy) in DIRECTIONS.items():
            nx, ny = x + dx, y + dy
            if self._at(nx, ny) is None:
                continue
            if JUMPS.get(self.behaviour(nx, ny)) == direction:
                if self.walkable(x + 2 * dx, y + 2 * dy):
                    yield direction, (x + 2 * dx, y + 2 * dy)
            elif self.walkable(nx, ny):
                yield direction, (nx, ny)

    def path(self, start: tuple[int, int], goal: tuple[int, int]) -> list[str] | None:
        """Shortest list of directions from start to goal (BFS), or None if unreachable."""
        previous: dict[tuple[int, int], tuple[tuple[int, int], str] | None] = {start: None}
        queue = deque([start])
        while queue:
            here = queue.popleft()
            if here == goal:
                steps = []
                while previous[here] is not None:
                    here, direction = previous[here]
                    steps.append(direction)
                return steps[::-1]
            for direction, there in self.moves(*here):
                if there not in previous:
                    previous[there] = (here, direction)
                    queue.append(there)
        return None

    def render(self, player: tuple[int, int], radius: int | None = None) -> str:
        """ASCII map: @ player, S sprite, W warp, . walkable, , grass, ~ water, v/</>/^ ledge, # blocked."""
        px, py = player
        xs = range(max(0, px - radius), min(self.width, px + radius + 1)) if radius else range(self.width)
        ys = range(max(0, py - radius), min(self.height, py + radius + 1)) if radius else range(self.height)
        rows = []
        for y in ys:
            row = []
            for x in xs:
                b = self.behaviour(x, y)
                if (x, y) == player:
                    row.append("@")
                elif (x, y) in self.sprites:
                    row.append("S")
                elif (x, y) in self.warps:
                    row.append("W")
                elif b in JUMPS:
                    row.append(LEDGE_MARKS[JUMPS[b]])
                elif b in WATER or b == WATERFALL:
                    row.append("~")
                elif self.walkable(x, y):
                    row.append("," if b in GRASS else ".")
                else:
                    row.append("#")
            rows.append(f"{y:3d} " + "".join(row))
        return f"    x from {xs.start}\n" + "\n".join(rows)


def layout_problems(read, layout: SimpleNamespace) -> list[str]:
    """The RAM invariants a running game breaks under `layout` (none: it fits). A game that has not started its main
    loop yet has zeros in these places and breaks none. `read(addr, n) -> bytes`."""
    bad = []
    callback = struct.unpack("<I", read(layout.MAIN + 4, 4))[0]
    if callback and not (0x08000000 <= callback < 0x0A000000 and callback & 1):
        bad.append("main callback")
    if layout.SB_POINTERS:
        for name, where in (("save block 1", layout.SB1), ("save block 2", layout.SB2)):
            pointer = struct.unpack("<I", read(where, 4))[0]
            if pointer and pointer not in EWRAM:
                bad.append(f"{name} pointer")
    return bad


def _sector_sizes(family: str) -> list[int]:
    sb2, sb1 = SAVE_BLOCKS[family]
    sizes = [min(sb2, SECTOR_DATA)] + [min(max(sb1 - i * SECTOR_DATA, 0), SECTOR_DATA) for i in range(4)]
    return sizes + [min(max(STORAGE_SIZE - i * SECTOR_DATA, 0), SECTOR_DATA) for i in range(9)]


def checksum(data: bytes) -> int:
    total = sum(struct.unpack_from(f"<{len(data) // 4}I", data)) & 0xFFFFFFFF
    return ((total >> 16) + total) & 0xFFFF


class Gen3:
    """Reads state and drives inputs for one English Ruby, Sapphire, Emerald, FireRed or LeafGreen ROM inside an
    `Emulator`. Names and tables come from the ROM; the RAM layout from the header's game code; the first reads of
    a running game check that layout against the game's own invariants (a hack that moved RAM stops there)."""

    ROMS, FAMILY, CORE, CORE_OPTIONS, BUTTONS = ROMS, FAMILY, CORE, CORE_OPTIONS, BUTTONS
    DEFAULT_OBJECTIVE, DEFAULT_CHECK, OBSERVATIONS = DEFAULT_OBJECTIVE, DEFAULT_CHECK, OBSERVATIONS
    TEAM_TYPES, GLITCHES, RAM_RANGES, WORK_RAM = TEAM_TYPES, GLITCHES, RAM_RANGES, WORK_RAM

    decode = staticmethod(decode)

    @classmethod
    def recognises(cls, rom: bytes) -> bool:
        return tables.find_tables(rom) is not None

    def __init__(self, rom: bytes):
        found = tables.find_tables(rom)
        if found is None:
            raise ValueError(f"unsupported ROM (sha1 {hashlib.sha1(rom).hexdigest()}): not an English Gen 3 game")
        sha1 = hashlib.sha1(rom).hexdigest()
        self.rom, self.found, self.family = rom, found, found["family"]
        self.title = ROMS.get(sha1) or f"Pokémon Gen 3 ({rom[0xAC:0xB0].decode()}, unlisted dump)"
        self.layout = LAYOUTS[self.family]
        self.functions = FUNCTIONS.get(sha1)  # None: RAM-only signals
        self.STARTERS = KANTO_STARTERS if self.family == "frlg" else HOENN_STARTERS
        self.SIDE_QUESTS = KANTO_QUESTS if self.family == "frlg" else HOENN_QUESTS
        self.bad_layout: list[str] = []
        self.species = tables.names(rom, found["species"], 11, tables.COUNTS["species"])
        self.moves = tables.names(rom, found["moves"], 13, tables.COUNTS["moves"])
        self.types = tables.names(rom, found["types"], 7, tables.COUNTS["types"])
        self.abilities = tables.names(rom, found["abilities"], 13, tables.COUNTS["abilities"])
        count = tables.ITEM_COUNTS[self.family]
        self.items = [tables.decode(rom[found["items"] + 44 * i : found["items"] + 44 * i + 14]) for i in range(count)]
        self.natures = tables.pointed_names(rom, found["natures"], tables.COUNTS["natures"]) if found["natures"] else []
        if self.family == "frlg":
            self.sections = tables.pointed_names(rom, found["maps"], 200)
            self.first_section = 0x58
        else:
            self.sections = tables.pointed_names(rom, found["maps"], 213, 8, 4)
            self.first_section = 0
        self._behaviours: dict[tuple[int, int], list[int]] = {}

    # ---- names ----

    def species_name(self, index: int) -> str:
        return self.species[index] if 0 < index < len(self.species) else f"#{index}"

    def item_name(self, index: int) -> str:
        return self.items[index] if 0 < index < len(self.items) else f"item {index}"

    def move_name(self, index: int) -> str:
        return self.moves[index] if 0 < index < len(self.moves) else ""

    def section_name(self, section: int) -> str:
        index = section - self.first_section
        return tables.clean(self.sections[index]) if 0 <= index < len(self.sections) else f"section {section}"

    # ---- low-level reads ----

    def _u32s(self, emu: Emulator, addr: int, n: int) -> tuple[int, ...]:
        return struct.unpack(f"<{n}I", emu.read(addr, 4 * n))

    def _blocks(self, emu: Emulator) -> tuple[int | None, int | None]:
        """The two save blocks' addresses (None for a pointer that does not point into EWRAM)."""
        L = self.layout
        if not L.SB_POINTERS:
            return L.SB1, L.SB2
        sb1, sb2 = self._u32s(emu, L.SB1, 1)[0], self._u32s(emu, L.SB2, 1)[0]
        return (sb1 if sb1 in EWRAM else None), (sb2 if sb2 in EWRAM else None)

    def check_layout(self, emu: Emulator) -> list[str]:
        """The invariants the running game breaks under this family's RAM layout, remembered once found."""
        if not self.bad_layout:
            self.bad_layout = layout_problems(emu.read, self.layout)
        return self.bad_layout

    def _tasks(self, emu: Emulator) -> list[int]:
        raw = emu.read(self.layout.TASKS, 16 * 0x28)
        return [struct.unpack_from("<I", raw, i * 0x28)[0] for i in range(16) if raw[i * 0x28 + 4]]

    def _script(self, emu: Emulator) -> tuple[int, int]:
        """The script engine's mode (0 stopped, 1 bytecode, 2 native) and the native function it waits on (stale, and
        so 0 here, unless the mode is 2)."""
        raw = emu.read(self.layout.SCRIPT, 8)
        return raw[1], struct.unpack_from("<I", raw, 4)[0] if raw[1] == 2 else 0

    def _printers(self, emu: Emulator) -> list[tuple[int, int, int]]:
        """(window, text pointer, state) of every active text printer. Emerald and FireRed/LeafGreen draw text with
        printers (state 0 printing, 1 pause, 2 and 3 waiting for A or B); Ruby and Sapphire with a message window
        (states mapped to the same numbers)."""
        L = self.layout
        if L.PRINTERS:
            raw = emu.read(L.PRINTERS, 32 * 0x24)
            return [
                (i, *struct.unpack_from("<I", raw, i * 0x24), raw[i * 0x24 + 0x1C])
                for i in range(32)
                if raw[i * 0x24 + 0x1B]
            ]
        window = emu.read(L.FIELD_WINDOW, 0x30)
        state = struct.unpack_from("<H", window, 0x16)[0]
        if not state:  # 0: finished
            return []
        waiting = state in (5, 8, 9)  # wait for a button, ▼ after a paragraph, ▼ after a scroll
        return [(0, struct.unpack_from("<I", window, 0x20)[0], 2 if waiting else 0)]

    def _controller(self, emu: Emulator) -> int:
        return self._u32s(emu, self.layout.CONTROLLERS, 1)[0]

    # ---- text ----

    def _message(self, emu: Emulator, pointer: int, battle: bool) -> str:
        """The paragraph of the message a printer is in (`pointer` is its next character)."""
        L = self.layout
        if 0x08000000 <= pointer < 0x0A000000:
            data, at = self.rom, pointer - 0x08000000
            start = at
            while start > 0 and data[start - 1] != tables.END and at - start < 600:
                start -= 1
            end = data.find(tables.END, at)
            text = data[start : end if end >= 0 else at + 1]
        else:
            where, size = L.BATTLE_TEXT if battle else L.STRING4
            if not where <= pointer < where + size:
                return ""
            text = emu.read(where, size)
            text = text[: text.find(tables.END) if tables.END in text else size]
            start = 0
            at = min(pointer - where, len(text))
        cut = at - start
        begin = text.rfind(bytes([tables.PARAGRAPH]), 0, max(cut - 1, 0)) + 1
        stop = text.find(bytes([tables.PARAGRAPH]), max(cut, begin))
        return tables.decode(text[begin : stop if stop >= 0 else len(text)] + bytes([tables.END])).strip()

    def _screen(self, emu: Emulator, in_battle: bool, in_game: bool) -> dict:
        """What the screen shows: mode overworld|dialog|menu|battle, plus prompt flags and the message text."""
        L, fns = self.layout, self.functions
        printers = self._printers(emu)
        script_mode, native = self._script(emu)
        callback = self._u32s(emu, L.MAIN + 4, 1)[0]
        tasks = self._tasks(emu) if fns else []
        yes_no = bool(fns) and any(t in fns["yes_no"] for t in tasks)
        menu_task = bool(fns) and any(t in fns["menu"] for t in tasks)
        printing = any(state in (0, 4, 5, 6) for _, _, state in printers)
        waiting = any(state in (1, 2, 3) for _, _, state in printers) or bool(fns and native == fns["wait"])
        controller = self._controller(emu) if in_battle else 0
        battle_menu = bool(fns) and controller in fns["action"] + fns["move"]
        if in_battle and not fns:  # RAM only: with no text on the way, the battle is waiting for a choice
            battle_menu = not printing and not waiting
        cursor = yes_no or menu_task or battle_menu
        if in_battle:
            kind = "battle"
        elif cursor:
            kind = "menu"
        elif printing or waiting or script_mode:  # a running script has the controls (a message, a cutscene)
            kind = "dialog"
        elif fns and callback and callback != fns["overworld"]:
            kind = "menu"  # a full-screen menu (bag, party, naming, title, ...)
        else:
            kind = "overworld"
        text = ""
        if printers or waiting or yes_no:  # a finished message stays on screen until A or B, or its prompt is answered
            if printers:
                pointer = printers[0][1]
            elif L.PRINTERS:
                pointer = struct.unpack_from("<I", emu.read(L.PRINTERS, 4))[0]
            else:
                pointer = struct.unpack_from("<I", emu.read(L.FIELD_WINDOW + 0x20, 4))[0]
            text = self._message(emu, pointer, in_battle) if pointer else ""
        return {
            "mode": kind,
            "cursor": cursor,
            "cursor_at": None,
            "yes_no": yes_no,
            "waiting": waiting,
            "printing": printing,
            "busy": bool(script_mode),
            "text": [line for line in text.split("\n") if line.strip()],
        }

    # ---- state ----

    def _mon(self, raw: bytes) -> dict | None:
        mon = decode_mon(raw)
        if mon is None or not mon["species"]:
            return None
        info = self.found["stats"] + 28 * mon["species"] if mon["species"] < len(self.species) else None
        base = self.rom[info : info + 28] if info is not None else bytes(28)
        kinds = list(dict.fromkeys(self.types[t] for t in base[6:8] if t < len(self.types)))
        abilities = [a for a in base[22:24] if a] or [0]
        slot = min(mon["ability_slot"], len(abilities) - 1)
        ups = [mon["pp_bonuses"] >> (2 * i) & 3 for i in range(4)]
        nature = mon["personality"] % 25
        status = mon.get("status", 0)
        return {
            "species": "EGG" if mon["egg"] else self.species_name(mon["species"]),
            "name": tables.decode(mon["nickname"]),
            "level": mon.get("level", 0),
            "hp": mon.get("hp", 0),
            "max_hp": mon.get("max_hp", 0),
            "status": "asleep" if status & 7 else next((name for bit, name in STATUS.items() if status & bit), "ok"),
            "types": kinds,
            "moves": [self.move_name(m) for m in mon["moves"] if m],
            "pp": [(pp, up) for m, pp, up in zip(mon["moves"], mon["pp"], ups, strict=True) if m],
            "exp": mon["exp"],
            "stats": mon.get("stats", []),  # Atk, Def, Spe, SpA, SpD
            "evs": mon["evs"],  # HP, Atk, Def, Spe, SpA, SpD
            "ivs": mon["ivs"],
            "nature": self.natures[nature] if nature < len(self.natures) else f"nature {nature}",
            "nature_up": NATURE_STATS[nature // 5] if nature // 5 != nature % 5 else "",
            "nature_down": NATURE_STATS[nature % 5] if nature // 5 != nature % 5 else "",
            "ability": self.abilities[abilities[slot]] if abilities[slot] < len(self.abilities) else "",
            "item": self.item_name(mon["item"]) if mon["item"] else "",
            "friendship": mon["friendship"],
        }

    def state(self, emu: Emulator) -> dict:
        L = self.layout
        problems = self.check_layout(emu)
        in_battle = bool(emu.read(L.MAIN + L.IN_BATTLE, 1)[0] >> 1 & 1)
        sb1, sb2 = self._blocks(emu)
        name = emu.read(sb2, L.PLAYER_NAME_SIZE) if sb2 else b""
        in_game = bool(sb1 and sb2 and valid_name(name) and not problems)
        state = {
            "map_id": 0,
            "map": "",
            "x": 0,
            "y": 0,
            "facing": "?",
            "map_size": [0, 0],
            "player_name": tables.decode(name) if in_game else "",
            "rival_name": "",
            "party": [],
            "bag": [],
            "money": 0,
            "badges": 0,
            "badge_bits": 0,
            "dex_owned": 0,
            "dex_seen": 0,
            "battle": None,
            "hall_of_fame": False,
            "in_game": in_game,
            "layout_problems": problems,
            "layout_unverified": False,
        }
        if not in_game:
            screen = self._screen(emu, in_battle, False)
            state |= {"screen": {k: v for k, v in screen.items() if k not in ("text", "printing", "busy")}}
            state["screen_text"] = screen["text"]
            return state
        header = emu.read(L.MAP_HEADER, 0x1C)
        x, y, group, number = struct.unpack("<hhbb", emu.read(sb1, 4) + emu.read(sb1 + 4, 2))
        layout = struct.unpack("<ii", emu.read(L.MAP_LAYOUT, 8))
        screen = self._screen(emu, in_battle, True)
        flags = emu.read(sb1 + L.FLAGS, 0x120)
        key = struct.unpack("<I", emu.read(sb2 + L.KEY, 4))[0] if L.KEY else 0
        count = min(emu.read(L.PARTY_COUNT, 1)[0], 6)
        raw = emu.read(L.PARTY, 100 * count) if count else b""
        party = [m for i in range(count) if (m := self._mon(raw[i * 100 : i * 100 + 100]))]
        bag = []
        for offset, size, pocket in L.POCKETS:
            slots = emu.read(sb1 + offset, 4 * size)
            for item, qty in (struct.unpack_from("<HH", slots, 4 * i) for i in range(size)):
                if item:
                    bag.append({"item": self.item_name(item), "qty": decode_quantity(qty, key), "pocket": pocket})
        badge_bits = sum(flag(flags, L.BADGES + i, 0) << i for i in range(8))
        dex = emu.read(sb2 + 0x18, 0x78)
        owned = int.from_bytes(dex[0x10:0x41], "little") & (1 << 386) - 1
        seen = int.from_bytes(dex[0x44:0x75], "little") & (1 << 386) - 1
        player = emu.read(L.AVATAR + 5, 1)[0]
        facing = FACING.get(emu.read(L.OBJECTS + player * 0x24 + 0x18, 1)[0] & 15, "?")
        state |= {
            "map_id": group << 8 | number,
            "map": self.section_name(header[0x14]),
            "x": x,
            "y": y,
            "facing": facing,
            "map_size": [layout[0] - 15, layout[1] - 14],
            "rival_name": tables.decode(emu.read(sb1 + L.RIVAL, 8)) if L.RIVAL else "",
            "party": party,
            "bag": bag,
            "money": decode_money(struct.unpack("<I", emu.read(sb1 + L.MONEY, 4))[0], key),
            "badges": badge_bits.bit_count(),
            "badge_bits": badge_bits,
            "dex_owned": owned.bit_count() if party else 0,
            "dex_seen": seen.bit_count(),
            "hall_of_fame": flag(flags, L.GAME_CLEAR, 0),
            "screen": {k: v for k, v in screen.items() if k not in ("text", "printing", "busy")},
            "screen_text": screen["text"],
        }
        if in_battle:
            state["battle"] = self._battle(emu)
        return state

    def _battle(self, emu: Emulator) -> dict:
        L = self.layout
        flags = struct.unpack("<I", emu.read(L.BATTLE_FLAGS, 4))[0]
        enemy = emu.read(L.BATTLE_MONS + 0x58, 0x58)  # battler 1: the opponent's active Pokémon
        species, hp, level, max_hp = struct.unpack_from("<H", enemy, 0)[0], *struct.unpack_from("<HBxH", enemy, 0x28)
        position = struct.unpack_from("<H", emu.read(L.BATTLE_PARTY_INDEX + 2, 2))[0]
        return {
            "kind": "trainer" if flags & 8 else "wild",
            "enemy": self.species_name(species),
            "enemy_level": level,
            "enemy_hp": hp,
            "enemy_pos": position,
            "enemy_max_hp": max_hp,
        }

    # ---- the map ----

    def _tileset_behaviours(self, layout_pointer: int) -> list[int]:
        """The behaviour of every metatile (primary tileset, then secondary) of the map layout at ROM `pointer`."""
        if layout_pointer in self._behaviours:
            return self._behaviours[layout_pointer]
        rom, frlg = self.rom, self.family == "frlg"
        base = layout_pointer - 0x08000000
        primary, secondary = (int.from_bytes(rom[base + o : base + o + 4], "little") - 0x08000000 for o in (0x10, 0x14))
        out: list[int] = []
        for tileset, count in ((primary, 640 if frlg else 512), (secondary, 384 if frlg else 512)):
            where = int.from_bytes(
                rom[tileset + (0x14 if frlg else 0x10) : tileset + (0x18 if frlg else 0x14)], "little"
            )
            where -= 0x08000000
            if frlg:
                out += [int.from_bytes(rom[where + 4 * i : where + 4 * i + 4], "little") & 0x1FF for i in range(count)]
            else:
                out += [int.from_bytes(rom[where + 2 * i : where + 2 * i + 2], "little") & 0xFF for i in range(count)]
        self._behaviours[layout_pointer] = out
        return out

    def grid(self, emu: Emulator) -> Grid:
        L = self.layout
        width, height, pointer = struct.unpack("<iiI", emu.read(L.MAP_LAYOUT, 12))
        raw = b"".join(
            emu.read(pointer + o, min(0x10000, 2 * width * height - o)) for o in range(0, 2 * width * height, 0x10000)
        )
        cells = list(struct.unpack(f"<{width * height}H", raw))
        header = emu.read(L.MAP_HEADER, 0x1C)
        rom_layout = struct.unpack_from("<I", header, 0)[0]
        metatiles = self._tileset_behaviours(rom_layout)
        behaviours = [metatiles[c & 0x3FF] if (c & 0x3FF) < len(metatiles) else 0 for c in cells]
        events = struct.unpack_from("<I", header, 4)[0] - 0x08000000
        warp_count = self.rom[events + 1]
        warps_at = int.from_bytes(self.rom[events + 8 : events + 12], "little") - 0x08000000
        warps = {struct.unpack_from("<hh", self.rom, warps_at + 8 * i) for i in range(min(warp_count, 64))}
        sprites = set()
        objects = emu.read(L.OBJECTS, 16 * 0x24)
        for i in range(16):
            o = objects[i * 0x24 : i * 0x24 + 0x24]
            if o[0] & 1 and not o[2] & 1 and not o[1] >> 5 & 1:  # active, not the player, not invisible
                ox, oy = struct.unpack_from("<hh", o, 0x10)
                sprites.add((ox - 7, oy - 7))
        surfing = bool(emu.read(L.AVATAR, 1)[0] & 8)
        return Grid(width - 15, height - 14, cells, behaviours, surfing, sprites, warps)

    # ---- scoring and checks ----

    @staticmethod
    def fingerprint(state: dict) -> dict:
        """Standard groups used for contracts and stuck detection."""
        return {
            "position": (state["map_id"], state["x"], state["y"]),
            "map": state["map_id"],
            "party": tuple((p["species"], p["level"], p["hp"], p["status"]) for p in state["party"]),
            "bag": tuple((b["item"], b["qty"]) for b in state["bag"]),
            "money": state["money"],
            "battle": None if not state["battle"] else (state["battle"]["enemy"], state["battle"]["enemy_hp"]),
            "menu": (state["screen"]["mode"], tuple(state["screen_text"])),
        }

    @staticmethod
    def progress_score(state: dict, maps_visited: int, tiles_visited: int) -> int:
        """Monotone-ish progress: badges dominate, then new maps, Pokédex, levels/experience and new tiles."""
        levels = sum(p["level"] for p in state["party"])
        return (
            state["badges"] * 10_000
            + maps_visited * 500
            + state["dex_owned"] * 200
            + levels * 50
            + sum(p["exp"] for p in state["party"]) // 100
            + tiles_visited
        )

    @staticmethod
    def observations(state: dict) -> dict:
        """Trusted values for success checks and conditions."""
        return {name: read(state) for name, read in OBSERVATIONS.items()}

    @staticmethod
    def _move_text(move: str, pp: tuple[int, int]) -> str:
        left, ups = pp
        return f"{move} {left}pp" + (f" ({ups} PP Up{'s' if ups > 1 else ''})" if ups else "")

    @staticmethod
    def state_text(state: dict, detail: bool = False) -> str:
        """The state as a few lines for the model; `detail` adds each party member's stats, nature, ability, EVs and
        IVs."""
        screen = state["screen"]
        flags = [f for f in ("yes_no", "waiting") if screen[f]]
        if state["layout_problems"]:
            return (
                "This game's memory layout does not match any this adapter knows (a ROM hack that moved RAM: "
                + ", ".join(state["layout_problems"])
                + " are not where they should be), so no state can be read. Only press, advance_text and the "
                "screenshot are meaningful."
            )
        text = "\n".join("  " + line for line in state["screen_text"]) or "  (no text)"
        if not state["in_game"]:
            return (
                f"No game is in progress yet (power-on, title screen, intro or naming), so map, party and other RAM"
                f" values are not meaningful. Screen: {screen['mode']}"
                + (f" ({', '.join(flags)})" if flags else "")
                + f".\nScreen text:\n{text}"
            )
        width, height = state["map_size"]
        lines = [
            f"Map: {state['map']} (id {state['map_id']:#06x}, {width}x{height} steps); you are at "
            f"({state['x']},{state['y']}) facing {state['facing']}. Screen: {screen['mode']}"
            + (f" ({', '.join(flags)})" if flags else ""),
            f"Player {state['player_name']}"
            + (f", rival {state['rival_name']}" if state["rival_name"] else "")
            + f". Money {state['money']}, badges {state['badges']}/8, Pokédex owned {state['dex_owned']} seen "
            f"{state['dex_seen']}.",
        ]
        party = [
            f"{p['name']} ({p['species']}, {'/'.join(p['types'])}) L{p['level']} HP {p['hp']}/{p['max_hp']} "
            f"{p['status']} {p['nature']}, {p['ability']}"
            + (f", holding {p['item']}" if p["item"] else "")
            + f" [{', '.join(Gen3._move_text(*move) for move in zip(p['moves'], p['pp'], strict=True))}]"
            for p in state["party"]
        ]
        lines.append("Party: " + ("; ".join(party) if party else "none"))
        if detail:
            lines += [
                f"  {p['name']}: {stat_line(STAT_NAMES, p['stats'])} ({p['nature']}: {nature_effect(p)}); "
                f"EVs {stat_line(EV_NAMES, p['evs'])}; IVs {stat_line(EV_NAMES, p['ivs'])}"
                for p in state["party"]
            ]
        bag = ", ".join(f"{b['item']} x{b['qty']}" for b in state["bag"]) or "empty"
        lines.append("Bag: " + bag)
        if battle := state["battle"]:
            lines.append(
                f"Battle ({battle['kind']}): {battle['enemy']} L{battle['enemy_level']} "
                f"HP {battle['enemy_hp']}/{battle['enemy_max_hp']}"
            )
        if state["screen_text"]:
            lines.append("Screen text:\n" + text)
        return "\n".join(lines)

    # ---- macro actions (controller input only) ----

    def press(self, emu: Emulator, button: str, hold: int = 4, wait: int = 12) -> None:
        emu.run([button], hold)
        emu.run([], wait)

    def _position(self, emu: Emulator) -> tuple[int, int, int]:
        """(map id, x, y) of the player; the map id is -1 while the save block is not there yet."""
        sb1, _ = self._blocks(emu)
        if sb1 is None:
            return -1, 0, 0
        x, y, group, number = struct.unpack("<hhbb", emu.read(sb1, 4) + emu.read(sb1 + 4, 2))
        return group << 8 | number, x, y

    def _moving(self, emu: Emulator) -> bool:
        avatar = emu.read(self.layout.AVATAR, 4)
        return bool(avatar[2] or avatar[3])  # running state, tile transition state

    def _settle(self, emu: Emulator, limit: int = 60) -> None:
        for _ in range(limit):
            if not self._moving(emu):
                return
            emu.run([], 1)

    def _settle_map(self, emu: Emulator, limit: int = 240) -> None:
        """After a warp the new map loads behind a fade: wait for the overworld to run again with input accepted."""
        fns = self.functions
        for _ in range(0, limit, 4):
            callback = self._u32s(emu, self.layout.MAIN + 4, 1)[0]
            ready = callback == fns["overworld"] if fns else True
            if ready and not self._moving(emu) and self._script(emu)[0] == 0:
                break
            emu.run([], 4)
        emu.run([], 8)

    def step(self, emu: Emulator, direction: str) -> str:
        """Move one step. Returns moved | blocked | map_changed | battle | dialog."""
        start = self._position(emu)
        for _ in range(30):
            emu.run([direction], 1)
            if self._position(emu) != start:
                break
        self._settle(emu)
        emu.run([], 2)
        now = self._position(emu)
        if now[0] != start[0]:
            self._settle_map(emu)
            now = self._position(emu)
        in_battle = bool(emu.read(self.layout.MAIN + self.layout.IN_BATTLE, 1)[0] >> 1 & 1)
        if in_battle:
            return "battle"
        if self._screen(emu, False, True)["mode"] != "overworld":
            return "dialog"
        if now[0] != start[0]:
            return "map_changed"
        return "moved" if now != start else "blocked"

    def walk(self, emu: Emulator, direction: str, steps: int) -> dict:
        done, outcome = 0, "moved"
        for _ in range(steps):
            outcome = self.step(emu, direction)
            if outcome == "blocked":
                break
            done += 1
            if outcome != "moved":
                break
        return {"steps": done, "outcome": outcome}

    def walk_to(self, emu: Emulator, x: int, y: int, max_steps: int = 300) -> dict:
        """Walk to (x, y) on the current map along a shortest path, re-planning after each step."""
        taken, replans = 0, 0
        while taken < max_steps:
            _, px, py = self._position(emu)
            if (px, py) == (x, y):
                return {"outcome": "arrived", "steps": taken}
            route = self.grid(emu).path((px, py), (x, y))
            if route is None:
                return {"outcome": "unreachable", "steps": taken}
            outcome = self.step(emu, route[0])
            if outcome == "blocked":
                replans += 1
                if replans > 5:
                    return {"outcome": "blocked", "steps": taken}
                continue
            taken += 1
            if outcome != "moved":
                return {"outcome": outcome, "steps": taken}
        arrived = self._position(emu)[1:] == (x, y)
        return {"outcome": "arrived" if arrived else "step_limit", "steps": taken}

    def advance_text(self, emu: Emulator, max_presses: int = 40, quiet_frames: int = 60) -> dict:
        """Press B through dialog until it closes. Stops, without pressing, at any menu or yes/no prompt.

        B, not A: both continue a message, but A talks to the person in front of you again once the box has closed.
        Text counts as closed once none has been on screen for `quiet_frames`.
        """
        presses = quiet = idle = 0
        for _ in range(max_presses * 40):
            in_battle = bool(emu.read(self.layout.MAIN + self.layout.IN_BATTLE, 1)[0] >> 1 & 1)
            screen = self._screen(emu, in_battle, True)
            if screen["cursor"]:
                return {"outcome": "menu", "presses": presses}
            if screen["waiting"]:
                quiet = 0
                if presses >= max_presses:
                    return {"outcome": "press_limit", "presses": presses}
                self.press(emu, "b", hold=2, wait=8)
                presses += 1
            elif screen["printing"]:
                quiet = 0
                emu.run(["b"], 6)  # B speeds a message up
            elif in_battle and not self.layout.PRINTERS:  # Ruby and Sapphire: no printer to watch, B moves it along
                if presses >= max_presses:
                    return {"outcome": "press_limit", "presses": presses}
                self.press(emu, "b", hold=2, wait=20)
                presses += 1
            elif screen["busy"] and not in_battle and idle < 600:  # a script is still running (a cutscene): wait for it
                idle += 4
                emu.run([], 4)
            else:
                quiet += 4
                if quiet >= quiet_frames:
                    return {"outcome": "closed" if presses else "no_text", "presses": presses}
                emu.run([], 4)
        return {"outcome": "timeout", "presses": presses}

    def save_game(self, emu: Emulator) -> bool:
        """Save in game as a player does: close text and back out of menus with B, then START, SAVE, YES, and YES again
        when an older save is replaced. False in a battle or at a yes/no prompt (never answered here), or when the
        menus did not follow."""
        for _ in range(12):
            state = self.state(emu)
            if not state["in_game"] or state["battle"] or state["screen"]["yes_no"]:
                return False
            if state["screen"]["mode"] == "overworld":
                break
            if state["screen"]["mode"] == "dialog":
                self.advance_text(emu)
            else:
                self.press(emu, "b", wait=30)
        else:
            return False
        L = self.layout
        sb1, _ = self._blocks(emu)
        flags = emu.read(sb1 + L.FLAGS, 0x120)
        # the Start menu lists POKéDEX, POKéMON, BAG, (POKéNAV,) the player, SAVE: each entry shown only once earned
        target = (
            flag(flags, L.POKEDEX_GET, 0)
            + flag(flags, L.POKEMON_GET, 0)
            + 1
            + (flag(flags, L.POKENAV_GET, 0) if L.POKENAV_GET else 0)
            + 1
        )
        self.press(emu, "start", wait=30)
        for _ in range(12):
            here = emu.read(L.START_CURSOR, 1)[0]
            if here == target:
                break
            self.press(emu, "down" if here < target else "up", wait=10)
        else:
            return False
        self.press(emu, "a", wait=60)
        answered = 0
        for _ in range(40):  # "Would you like to save the game?", then maybe "There is already a save file. Overwrite?"
            screen = self._screen(emu, False, True)
            if screen["mode"] == "overworld":
                return bool(answered)
            if screen["waiting"]:
                self.press(emu, "b", wait=20)
            elif screen["cursor"] and not screen["printing"]:
                # the question's own YES/NO is no task of its own, but the Start menu's: YES is the first entry
                self.press(emu, "a", wait=60)
                answered += 1
            else:
                emu.run([], 30)
        return False

    def save_complete(self, sram: bytes) -> bool:
        """Whether cartridge RAM holds a whole in-game save: the right size and, in one of the two save slots, all 14
        sectors of one save with the game's signature, ids and checksums."""
        if len(sram) != SAVE_SIZE:
            return False
        sizes = _sector_sizes(self.family)
        for slot in range(2):
            sectors = [sram[(14 * slot + i) * SECTOR : (14 * slot + i + 1) * SECTOR] for i in range(14)]
            footers = [struct.unpack_from("<HHII", s, 0xFF4) for s in sectors]  # id, checksum, signature, counter
            if len({f[3] for f in footers}) != 1 or sorted(f[0] for f in footers) != list(range(14)):
                continue
            if all(
                f[2] == SECTOR_SIGNATURE and f[1] == checksum(s[: sizes[f[0]]])
                for f, s in zip(footers, sectors, strict=True)
            ):
                return True
        return False
