"""Game adapter for the English Pokémon Red, Blue and Yellow (Game Boy), and hacks built on their engine.

RAM addresses come from the pret/pokered and pret/pokeyellow disassemblies: Red and Blue share one layout, Yellow's is
the same minus one byte from `WALK_COUNTER` on. Pokémon, item and move names and the collision tables are found in the
user's ROM at runtime (`gen1_tables`), so no game text is stored in this repository and a hack that keeps the engine
works without an entry here. All game progress goes through controller input; RAM is only read.
"""

import hashlib
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

from pokeagent.emulator.client import Emulator
from pokeagent.games.gen1_tables import find_tables

ROMS = {
    "ea9bcae617fdf159b045185467ae58b2e4a48b9a": "Pokémon Red (English)",
    "d7037c83e1ae5b39bde3c30787637ba1d4c48ce2": "Pokémon Blue (English)",
    "cc7d03262ebfaf2f06772c1a480c7d9d5f4a38e1": "Pokémon Yellow (English)",
}
FAMILY = "pokemon-gen1"
CORE = "sameboy"
CORE_OPTIONS = {"sameboy_model": "Game Boy"}
YELLOW_CORE_OPTIONS = {"sameboy_model": "Game Boy Color"}  # Yellow is a colour-enhanced cartridge
HALL_OF_FAME = 0x76
DEFAULT_OBJECTIVE = "Finish the game: defeat the Elite Four and the Champion and enter the Hall of Fame."
DEFAULT_CHECK = {"obs": "map_id", "op": "==", "value": HALL_OF_FAME}
MAP_NAMES = Path(__file__).with_name("gen1_map_names.txt").read_text().splitlines()
YELLOW_MAP_NAMES = Path(__file__).with_name("gen1_yellow_map_names.txt").read_text().splitlines()
# Per-run variety for the natural play style: the starters (species index: type ids), the type ids a team can
# draw (all but BIRD and the unused ids) and optional side quests.
STARTERS = {0x99: (0x16, 0x03), 0xB0: (0x14,), 0xB1: (0x15,)}
YELLOW_STARTERS = {0x54: (0x17,)}  # PIKACHU is the only starter (ELECTRIC)
TEAM_TYPES = (0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x07, 0x08, 0x14, 0x15, 0x16, 0x17, 0x18, 0x19, 0x1A)
SIDE_QUESTS = (
    "Buy the Magikarp the salesman offers in the Mt. Moon Pokémon Center and raise it into a Gyarados.",
    "Get the Coin Case in Celadon City and win a Pokémon prize at the Game Corner.",
    "Catch a Safari Zone Pokémon that lives nowhere else, such as Chansey, Kangaskhan or Tauros.",
    "Make at least two of the trades that people in towns offer.",
    "Collect the Old Rod, the Good Rod and the Super Rod, and catch a Pokémon with each.",
    "Revive a fossil at the Cinnabar Island lab.",
    "Receive the gift Eevee in Celadon Mansion and evolve it with a stone of your choice.",
    "Receive the gift Lapras at Silph Co.",
    "Catch one of the legendary birds: Articuno, Zapdos or Moltres.",
    "Win Hitmonlee or Hitmonchan at the fighting dojo in Saffron City.",
    "Leave a Pokémon at the Route 5 Day Care for a while and collect it again.",
    "Show Professor Oak's aides enough caught species to receive HM05 Flash, the Itemfinder and the Exp. All.",
    "Catch a Snorlax instead of defeating it.",
    "Battle every trainer on the routes you cross.",
    "Catch a Pokémon of every type.",
    "Catch a Pikachu.",
    "Get the Bicycle and ride down Cycling Road.",
)
GLITCHES = (
    "Glitches: allowed in this run. Known glitches of this game done with ordinary controller input are fair play,"
    " such as the Mew glitch, MissingNo. and item duplication (the old man in Viridian City, then the east coast of"
    " Cinnabar Island) and arbitrary code execution with the glitch item 8F. Research the exact steps before you try"
    " one: some corrupt the save, the Hall of Fame or the game, so use them on purpose and record what worked."
)
# Yellow: the same quests, minus the Safari Zone (its wild Pokémon differ) and the Pikachu that follows you anyway.
YELLOW_SIDE_QUESTS = tuple(q for q in SIDE_QUESTS if "Safari" not in q and q != "Catch a Pikachu.")
YELLOW_GLITCHES = (
    "Glitches: allowed in this run. Known glitches of this game done with ordinary controller input are fair play."
    " Research the exact steps before you try one: some corrupt the save, the Hall of Fame or the game, so use them"
    " on purpose and record what worked."
)
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
    "in_game": lambda s: s["in_game"],
}

# WRAM / HRAM (pret pokered symbols; the values are Red/Blue's, `RAM_LAYOUTS` has Yellow's)
TILEMAP = 0xC3A0  # 20x18 screen tile ids
SPRITES1, SPRITES2 = 0xC100, 0xC200  # 16 sprites x 16 bytes each; sprite 0 is the player
OVERWORLD_MAP = 0xC6E8  # current map's blocks with a 3-block border
JOY_IGNORE = 0xCD6B
WALK_COUNTER = 0xCFC5
ENEMY_SPECIES, ENEMY_HP, ENEMY_LEVEL, ENEMY_MAX_HP = 0xCFE5, 0xCFE6, 0xCFF3, 0xCFF4
IS_IN_BATTLE = 0xD057
PLAYER_NAME, RIVAL_NAME = 0xD158, 0xD34A
PARTY_COUNT, PARTY_SPECIES, PARTY_MONS, PARTY_NICKS = 0xD163, 0xD164, 0xD16B, 0xD2B5
DEX_OWNED, DEX_SEEN = 0xD2F7, 0xD30A
NUM_BAG_ITEMS, BAG_ITEMS = 0xD31D, 0xD31E
MONEY, BADGES = 0xD347, 0xD356
CUR_MAP = 0xD35E  # map, view pointer (2), y, x, ..., tileset (+9), height (+10), width (+11)
NUM_WARPS, WARPS = 0xD3AE, 0xD3AF
TILESET_BANK, TILESET_BLOCKS, TILESET_COLLISION, GRASS_TILE = 0xD52B, 0xD52C, 0xD530, 0xD535
# Yellow has one byte less WRAM before WALK_COUNTER, so every constant from there on sits one lower.
SHIFTED = [
    "WALK_COUNTER",
    "ENEMY_SPECIES",
    "ENEMY_HP",
    "ENEMY_LEVEL",
    "ENEMY_MAX_HP",
    "IS_IN_BATTLE",
    "PLAYER_NAME",
    "RIVAL_NAME",
    "PARTY_COUNT",
    "PARTY_SPECIES",
    "PARTY_MONS",
    "PARTY_NICKS",
    "DEX_OWNED",
    "DEX_SEEN",
    "NUM_BAG_ITEMS",
    "BAG_ITEMS",
    "MONEY",
    "BADGES",
    "CUR_MAP",
    "NUM_WARPS",
    "WARPS",
    "TILESET_BANK",
    "TILESET_BLOCKS",
    "TILESET_COLLISION",
    "GRASS_TILE",
]
RAM_LAYOUTS = {
    "red-blue": {name: globals()[name] for name in SHIFTED},
    "yellow": {name: globals()[name] - 1 for name in SHIFTED},
}
PIKACHU_SLOT = 15  # Yellow: the follower's sprite slot (wSpritePikachuStateData1); it never blocks the player

CHARMAP = {0x50: "", 0x7F: " ", 0xBA: "é", 0xBB: "'d", 0xBC: "'l", 0xBD: "'s", 0xBE: "'t", 0xBF: "'v", 0xE0: "'"}
CHARMAP |= {0xE1: "PK", 0xE2: "MN", 0xE3: "-", 0xE4: "'r", 0xE5: "'m", 0xE6: "?", 0xE7: "!", 0xE8: ".", 0xEC: "▷"}
CHARMAP |= {0xED: "▶", 0xEE: "▼", 0xEF: "♂", 0xF0: "¥", 0xF1: "×", 0xF2: ".", 0xF3: "/", 0xF4: ",", 0xF5: "♀"}  # noqa: RUF001 (the game's own symbol)
CHARMAP |= {0x79: "┌", 0x7A: "─", 0x7B: "┐", 0x7C: "│", 0x7D: "└", 0x7E: "┘", 0x9A: "(", 0x9B: ")", 0x9C: ":"}
CHARMAP |= {0x9D: ";", 0x9E: "[", 0x9F: "]", 0x6D: ":", 0x75: "…", 0x4A: "PKMN", 0x54: "POKé", 0x5D: "TRAINER"}
CHARMAP |= {0x80 + i: chr(ord("A") + i) for i in range(26)} | {0xA0 + i: chr(ord("a") + i) for i in range(26)}
CHARMAP |= {0xF6 + i: str(i) for i in range(10)}
CURSOR, WAIT_ARROW, BOX_TOP_LEFT, BOX_BOTTOM_RIGHT = 0xED, 0xEE, 0x79, 0x7E
STATUS = {0x08: "poisoned", 0x10: "burned", 0x20: "frozen", 0x40: "paralyzed"}

REACT_FRAMES = 32
# The in-game save in cartridge RAM bank 1 (pret pokered sPlayerName .. sMainDataCheckSum): the game sums these bytes,
# complements the sum and stores it after them; a save whose checksum fails loads as "The file data is destroyed!"
SAVE_SIZE, SAVE_MAIN, SAVE_CHECKSUM = 0x8000, slice(0x2598, 0x3523), 0x3523
DIRECTIONS = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}
FACING_CODES = {0x0: "down", 0x4: "up", 0x8: "left", 0xC: "right"}


def decode(data: bytes) -> str:
    """Game text to str; stops at the 0x50 terminator. Unknown codes become '·'."""
    out = []
    for b in data:
        if b == 0x50:
            break
        out.append(CHARMAP.get(b, "·"))
    return "".join(out)


def bcd(data: bytes) -> int:
    digits = "".join(f"{b:02x}" for b in data)
    return int(digits) if digits.isdigit() else 0  # not BCD: power-on noise before a game is loaded


def stat_line(values: list[int], hp: bool = False) -> str:
    """Atk, Def, Spd, Spc (and HP) values as "Atk 9 Def 12 ...", the order party entries keep them in."""
    return " ".join(f"{name} {value}" for name, value in zip(("Atk", "Def", "Spd", "Spc", "HP"), values, strict=False))


def dvs(first: int, second: int) -> list[int]:
    """Attack, Defense, Speed, Special and HP DVs (0-15) from the two DV bytes; HP's comes from the others' low bits."""
    atk, dfn, spd, spc = first >> 4, first & 15, second >> 4, second & 15
    return [atk, dfn, spd, spc, (atk & 1) << 3 | (dfn & 1) << 2 | (spd & 1) << 1 | spc & 1]


@dataclass
class Grid:
    """Walkability of the current map in player steps (2x2 tiles), from the map's blocks and the tileset's rules."""

    width: int
    height: int
    tiles: list[list[int]]  # lower-left tile id of each step square; the game checks this tile
    passable: frozenset[int]
    tile_pairs: frozenset[tuple[int, int]]  # (from tile, to tile) moves blocked in this tileset
    ledges: dict[tuple[str, int, int], str]  # (direction, standing tile, ledge tile) -> direction of the jump
    sprites: set[tuple[int, int]] = field(default_factory=set)
    warps: set[tuple[int, int]] = field(default_factory=set)
    grass: int | None = None

    def inside(self, x: int, y: int) -> bool:
        return 0 <= x < self.width and 0 <= y < self.height

    def walkable(self, x: int, y: int) -> bool:
        return (
            self.inside(x, y)
            and (x, y) not in self.sprites
            and (self.tiles[y][x] in self.passable or (x, y) in self.warps)
        )

    def moves(self, x: int, y: int):
        """Yield (direction, destination) for every legal single move from (x, y), ledge jumps included."""
        for direction, (dx, dy) in DIRECTIONS.items():
            nx, ny = x + dx, y + dy
            if not self.inside(nx, ny):
                continue
            here, there = self.tiles[y][x], self.tiles[ny][nx]
            if (direction, here, there) in self.ledges and self.walkable(x + 2 * dx, y + 2 * dy):
                yield direction, (x + 2 * dx, y + 2 * dy)
            elif self.walkable(nx, ny) and (here, there) not in self.tile_pairs:
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
        """ASCII map: @ player, S sprite, W warp, . walkable, , grass, v/</>/^ ledge, # blocked."""
        ledge_marks = {"down": "v", "left": "<", "right": ">", "up": "^"}
        ledge_tiles = {(here_dir, ledge): ledge_marks[here_dir] for (here_dir, _, ledge) in self.ledges}
        px, py = player
        xs = range(max(0, px - radius), min(self.width, px + radius + 1)) if radius else range(self.width)
        ys = range(max(0, py - radius), min(self.height, py + radius + 1)) if radius else range(self.height)
        rows = []
        for y in ys:
            row = []
            for x in xs:
                tile = self.tiles[y][x]
                if (x, y) == player:
                    row.append("@")
                elif (x, y) in self.sprites:
                    row.append("S")
                elif (x, y) in self.warps:
                    row.append("W")
                elif tile in self.passable:
                    row.append("," if tile == self.grass else ".")
                else:
                    row.append(next((m for (_, ledge), m in ledge_tiles.items() if ledge == tile), "#"))
            rows.append(f"{y:3d} " + "".join(row))
        return f"    x from {xs.start}\n" + "\n".join(rows)


def layout_problems(read, layout: dict[str, int], banks: int = 64) -> list[str]:
    """The RAM invariants a loaded game breaks under `layout` (none: it fits). Needs a game in progress: power-on RAM
    is noise and fails every layout. `read(addr, n) -> bytes`."""
    bad = []
    name = read(layout["PLAYER_NAME"], 11)
    end = name.find(0x50)
    if not (1 <= end <= 10 and all(0x7F <= b <= 0xF5 for b in name[:end])):
        bad.append("player name")
    count, species = read(layout["PARTY_COUNT"], 1)[0], read(layout["PARTY_SPECIES"], 7)
    if count > 6 or species[count] != 0xFF or not all(1 <= b <= 190 for b in species[:count]):
        bad.append("party list")
    items = read(layout["NUM_BAG_ITEMS"], 1)[0]
    if items > 20 or read(layout["BAG_ITEMS"] + 2 * items, 1)[0] != 0xFF:
        bad.append("bag list")
    m = read(layout["CUR_MAP"], 12)
    if not (m[0] < 0xF8 and 1 <= m[10] <= 60 and 1 <= m[11] <= 60 and m[9] < 25):
        bad.append("map header")
    t = read(layout["TILESET_BANK"], 7)
    blocks, collision = int.from_bytes(t[1:3], "little"), int.from_bytes(t[5:7], "little")
    if not (0 < t[0] < banks and 0x4000 <= blocks < 0x8000 and collision < 0x8000):
        bad.append("tileset pointers")
    return bad


class Gen1:
    """Reads state and drives inputs for one English Red, Blue or Yellow ROM (or a hack of their engine) inside an
    `Emulator`. The version comes from the ROM itself: its tables are scanned for, the base game is guessed from the
    header title and table banks, and the first state read of a running game checks the WRAM layout against the
    game's own invariants."""

    ROMS, FAMILY, CORE, CORE_OPTIONS = ROMS, FAMILY, CORE, CORE_OPTIONS
    DEFAULT_OBJECTIVE, DEFAULT_CHECK, OBSERVATIONS = DEFAULT_OBJECTIVE, DEFAULT_CHECK, OBSERVATIONS
    STARTERS, TEAM_TYPES, SIDE_QUESTS, GLITCHES = STARTERS, TEAM_TYPES, SIDE_QUESTS, GLITCHES

    decode = staticmethod(decode)

    @classmethod
    def recognises(cls, rom: bytes) -> bool:
        return find_tables(rom) is not None

    def __init__(self, rom: bytes):
        tables = find_tables(rom)
        if tables is None:
            raise ValueError(f"unsupported ROM (sha1 {hashlib.sha1(rom).hexdigest()}): not an English Gen 1 game")
        header = rom[0x134:0x144].split(b"\0")[0].decode("ascii", "replace")
        self.title = ROMS.get(hashlib.sha1(rom).hexdigest()) or f"Gen 1 ROM hack (header title {header})"
        self.rom = rom
        if rom[0x143] & 0x80:  # colour support is hardware, not ancestry: Red/Blue hacks may set it too
            self.CORE_OPTIONS = YELLOW_CORE_OPTIONS
        self.layout_checked = False  # set once a running game has confirmed (or replaced) the base game
        # first guess at the base game: Yellow keeps its name tables in bank 0x3A, Red and Blue in bank 7
        self._set_base("YELLOW" in header or tables["species"][0] == 0x3A)
        self.species = [decode(self._rom(tables["species"], 190 * 10)[i * 10 : i * 10 + 10]) for i in range(190)]
        self.items = self._names(tables["items"], 0x61)
        self.moves = self._names(tables["moves"], 165)
        bank, _ = tables["types"]
        pointers = self._rom(tables["types"], 2 * 0x1B)
        self.types = [self._names((bank, pointers[2 * i] | pointers[2 * i + 1] << 8), 1)[0] for i in range(0x1B)]
        self.tile_pairs = {}
        for tileset, a, b in self._table(tables["tile_pairs"], 3):
            self.tile_pairs.setdefault(tileset, set()).update({(a, b), (b, a)})
        self.ledges = {
            (FACING_CODES[f], standing, ledge): FACING_CODES[f]
            for f, standing, ledge, _ in self._table(tables["ledges"], 4)
        }

    def _set_base(self, yellow: bool) -> None:
        """Select everything that depends on the base game (RAM layout, starters, map names, flavour text)."""
        self.yellow = yellow
        self.layout = "yellow" if yellow else "red-blue"
        self.ram = SimpleNamespace(**RAM_LAYOUTS[self.layout])
        if yellow:
            self.STARTERS, self.SIDE_QUESTS = YELLOW_STARTERS, YELLOW_SIDE_QUESTS
            self.GLITCHES, self.map_names = YELLOW_GLITCHES, YELLOW_MAP_NAMES
        else:
            self.STARTERS, self.SIDE_QUESTS, self.GLITCHES, self.map_names = STARTERS, SIDE_QUESTS, GLITCHES, MAP_NAMES

    def check_layout(self, emu: Emulator) -> None:
        """Once a game runs, confirm the WRAM layout the header suggested, or switch to the one that fits (hacks)."""
        if self.layout_checked:
            return
        fits = [name for name, layout in RAM_LAYOUTS.items() if not layout_problems(emu.read, layout)]
        if self.layout in fits or len(fits) == 1:
            self._set_base((self.layout if self.layout in fits else fits[0]) == "yellow")
            self.layout_checked = True

    def _offset(self, where: tuple[int, int]) -> int:
        bank, addr = where
        return addr if bank == 0 else bank * 0x4000 + addr - 0x4000

    def _rom(self, where: tuple[int, int], n: int) -> bytes:
        start = self._offset(where)
        return self.rom[start : start + n]

    def _names(self, where: tuple[int, int], count: int) -> list[str]:
        start, names = self._offset(where), []
        for _ in range(count):
            end = self.rom.index(0x50, start)
            names.append(decode(self.rom[start:end]))
            start = end + 1
        return names

    def _table(self, where: tuple[int, int], width: int) -> list[tuple[int, ...]]:
        start, rows = self._offset(where), []
        while self.rom[start] != 0xFF:
            rows.append(tuple(self.rom[start : start + width]))
            start += width
        return rows

    def species_name(self, index: int) -> str:
        return self.species[index - 1] if 1 <= index <= len(self.species) else f"#{index}"

    def item_name(self, index: int) -> str:
        if 0xC4 <= index <= 0xC8:
            return f"HM{index - 0xC3:02d}"
        if index >= 0xC9:
            return f"TM{index - 0xC8:02d}"
        return self.items[index - 1] if 1 <= index <= len(self.items) else f"item {index:#04x}"

    def move_name(self, index: int) -> str:
        return self.moves[index - 1] if 1 <= index <= len(self.moves) else ""

    # ---- state ----

    @staticmethod
    def screen_tiles(emu: Emulator) -> bytes:
        return emu.read(TILEMAP, 20 * 18)

    @staticmethod
    def screen_text(tiles: bytes) -> list[str]:
        return [decode(tiles[r * 20 : (r + 1) * 20]).replace("·", " ").rstrip() for r in range(18)]

    @staticmethod
    def screen_mode(tiles: bytes, in_battle: bool) -> dict:
        """What the screen shows: mode overworld|dialog|menu|battle, plus prompt flags."""
        cursor = CURSOR in tiles
        dialog = tiles[12 * 20] == BOX_TOP_LEFT and tiles[17 * 20 + 19] == BOX_BOTTOM_RIGHT
        text = "\n".join(Gen1.screen_text(tiles))
        yes_no = cursor and "YES" in text and "NO" in text
        mode = "battle" if in_battle else "menu" if cursor else "dialog" if dialog else "overworld"
        at = tiles.find(CURSOR)
        return {
            "mode": mode,
            "cursor": cursor,
            "cursor_at": [at % 20, at // 20] if cursor else None,
            "yes_no": yes_no,
            "waiting": tiles[16 * 20 + 18] == WAIT_ARROW,
        }

    def state(self, emu: Emulator) -> dict:
        self.check_layout(emu)
        m = emu.read(self.ram.CUR_MAP, 12)
        map_id, y, x = m[0], m[3], m[4]
        tiles = self.screen_tiles(emu)
        battle_kind = emu.read(self.ram.IS_IN_BATTLE, 1)[0]
        facing = FACING_CODES.get(emu.read(SPRITES1 + 9, 1)[0], "?")
        party = []
        count = min(emu.read(self.ram.PARTY_COUNT, 1)[0], 6)
        mons, nicks = emu.read(self.ram.PARTY_MONS, 44 * 6), emu.read(self.ram.PARTY_NICKS, 11 * 6)
        for i in range(count):
            mon = mons[i * 44 : i * 44 + 44]
            status = mon[4]
            party.append(
                {
                    "species": self.species_name(mon[0]),
                    "name": decode(nicks[i * 11 : i * 11 + 11]),
                    "level": mon[33],
                    "hp": int.from_bytes(mon[1:3]),
                    "max_hp": int.from_bytes(mon[34:36]),
                    "status": "asleep" if status & 7 else STATUS.get(status & 0xF8, "ok" if not status else "status"),
                    "types": list(dict.fromkeys(self.types[t] for t in mon[5:7] if t < len(self.types))),
                    "moves": [self.move_name(mv) for mv in mon[8:12] if mv],
                    "pp": [(pp & 0x3F, pp >> 6) for mv, pp in zip(mon[8:12], mon[29:33], strict=True) if mv],
                    "exp": int.from_bytes(mon[14:17]),
                    # Atk, Def, Spd, Spc, then HP for stat exp and DVs (`stat_line` labels them)
                    "stats": [int.from_bytes(mon[i : i + 2]) for i in (36, 38, 40, 42)],
                    "stat_exp": [int.from_bytes(mon[i : i + 2]) for i in (19, 21, 23, 25, 17)],
                    "dvs": dvs(mon[27], mon[28]),
                }
            )
        bag = []
        raw = emu.read(self.ram.BAG_ITEMS, 2 * 20)
        for i in range(min(emu.read(self.ram.NUM_BAG_ITEMS, 1)[0], 20)):
            bag.append({"item": self.item_name(raw[2 * i]), "qty": raw[2 * i + 1]})
        battle = None
        if battle_kind:
            e = emu.read(self.ram.ENEMY_SPECIES, 16)
            battle = {
                "kind": {1: "wild", 2: "trainer"}.get(battle_kind, "lost"),
                "enemy": self.species_name(e[0]),
                "enemy_level": emu.read(self.ram.ENEMY_LEVEL, 1)[0],
                "enemy_hp": int.from_bytes(e[1:3]),
                "enemy_pos": e[3],  # wEnemyMonPartyPos: which party member of the enemy is out
                "enemy_max_hp": int.from_bytes(emu.read(self.ram.ENEMY_MAX_HP, 2)),
            }
        badges = emu.read(self.ram.BADGES, 1)[0]
        player_name = decode(emu.read(self.ram.PLAYER_NAME, 11))
        in_game = bool(m[10] and m[11]) and bool(player_name) and "·" not in player_name
        return {
            "map_id": map_id,
            "map": self.map_names[map_id] if map_id < len(self.map_names) else f"map {map_id:#04x}",
            "x": x,
            "y": y,
            "facing": facing,
            "map_size": [m[11] * 2, m[10] * 2],
            "screen": self.screen_mode(tiles, bool(battle_kind)),
            "screen_text": [line for line in self.screen_text(tiles) if line.strip()],
            "player_name": player_name,
            "rival_name": decode(emu.read(self.ram.RIVAL_NAME, 11)),
            "party": party,
            "bag": bag,
            "money": bcd(emu.read(self.ram.MONEY, 3)),
            "badges": badges.bit_count(),
            "badge_bits": badges,
            # Looking at a starter's ball in Oak's Lab sets four owned flags while its Pokédex entry is shown (seen as
            # a "4 species owned" milestone in live run 2); nothing is owned before the party has a member.
            "dex_owned": sum(b.bit_count() for b in emu.read(self.ram.DEX_OWNED, 19)) if party else 0,
            "dex_seen": sum(b.bit_count() for b in emu.read(self.ram.DEX_SEEN, 19)),
            "battle": battle,
            # Before a game is started or loaded, WRAM holds power-on noise (SameBoy randomises it) or zeros.
            "in_game": in_game,
            # a running game that fits no known layout (a hack that moved WRAM): the values may be wrong
            "layout_unverified": in_game and not self.layout_checked,
        }

    def grid(self, emu: Emulator) -> Grid:
        m = emu.read(self.ram.CUR_MAP, 12)
        tileset, height, width = m[9], m[10], m[11]
        stride = width + 6
        blocks = emu.read(OVERWORLD_MAP, stride * (height + 6))
        t = emu.read(self.ram.TILESET_BANK, 7)
        bank, blocks_ptr, coll_ptr = t[0], int.from_bytes(t[1:3], "little"), int.from_bytes(t[5:7], "little")
        passable = frozenset(row[0] for row in self._table((0, coll_ptr), 1))
        tiles = [[0] * width * 2 for _ in range(height * 2)]
        for by in range(height):
            for bx in range(width):
                block = blocks[(by + 3) * stride + bx + 3]
                cells = self._rom((bank, blocks_ptr + block * 16), 16)
                for sy in range(2):
                    for sx in range(2):
                        tiles[by * 2 + sy][bx * 2 + sx] = cells[(2 * sy + 1) * 4 + 2 * sx]
        sprites = set()
        s1, s2 = emu.read(SPRITES1, 256), emu.read(SPRITES2, 256)
        for i in range(1, PIKACHU_SLOT if self.yellow else 16):  # Yellow's follower never blocks
            if s1[i * 16] and s1[i * 16 + 2] != 0xFF:
                sprites.add((s2[i * 16 + 5] - 4, s2[i * 16 + 4] - 4))
        n = min(emu.read(self.ram.NUM_WARPS, 1)[0], 32)
        raw = emu.read(self.ram.WARPS, 4 * n) if n else b""
        warps = {(raw[4 * i + 1], raw[4 * i]) for i in range(n)}
        return Grid(
            width * 2,
            height * 2,
            tiles,
            passable,
            frozenset(self.tile_pairs.get(tileset, ())),
            self.ledges if tileset == 0 else {},  # ledges exist only in the overworld tileset
            sprites,
            warps,
            emu.read(self.ram.GRASS_TILE, 1)[0],
        )

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
    def state_text(state: dict, detail: bool = False) -> str:
        """The state as a few lines for the model; `detail` adds each party member's stats, stat exp and DVs."""
        screen = state["screen"]
        flags = [f for f in ("yes_no", "waiting") if screen[f]]
        if not state["in_game"]:
            text = "\n".join("  " + line for line in state["screen_text"]) or "  (no text)"
            return (
                f"No game is in progress yet (power-on, title screen, intro or naming), so map, party and other RAM"
                f" values are not meaningful. Screen: {screen['mode']}"
                + (f" ({', '.join(flags)})" if flags else "")
                + f".\nScreen text:\n{text}"
            )
        width, height = state["map_size"]
        lines = ["Note: this game's memory layout is unknown; the values below may be wrong."] * state.get(
            "layout_unverified"
        )
        lines += [
            f"Map: {state['map']} (id {state['map_id']:#04x}, {width}x{height} steps); you are at "
            f"({state['x']},{state['y']}) facing {state['facing']}. Screen: {screen['mode']}"
            + (f" ({', '.join(flags)})" if flags else "")
            + (f", cursor at column {screen['cursor_at'][0]} row {screen['cursor_at'][1]}" if screen["cursor"] else ""),
            f"Player {state['player_name']}, rival {state['rival_name']}. Money {state['money']}, badges "
            f"{state['badges']}/8, Pokédex owned {state['dex_owned']} seen {state['dex_seen']}.",
        ]
        party = [
            f"{p['name']} ({p['species']}, {'/'.join(p['types'])}) L{p['level']} HP {p['hp']}/{p['max_hp']} "
            f"{p['status']} [{', '.join(Gen1._move_text(*move) for move in zip(p['moves'], p['pp'], strict=True))}]"
            for p in state["party"]
        ]
        lines.append("Party: " + ("; ".join(party) if party else "none"))
        if detail:
            lines += [
                f"  {p['name']}: {stat_line(p['stats'])}; stat exp {stat_line(p['stat_exp'], hp=True)};"
                f" DVs {stat_line(p['dvs'], hp=True)}"
                for p in state["party"]
            ]
        lines.append("Bag: " + (", ".join(f"{b['item']} x{b['qty']}" for b in state["bag"]) or "empty"))
        if battle := state["battle"]:
            lines.append(
                f"Battle ({battle['kind']}): {battle['enemy']} L{battle['enemy_level']} "
                f"HP {battle['enemy_hp']}/{battle['enemy_max_hp']}"
            )
        if state["screen_text"]:
            lines.append("Screen text:\n" + "\n".join("  " + line for line in state["screen_text"]))
        return "\n".join(lines)

    @staticmethod
    def _move_text(move: str, pp: tuple[int, int]) -> str:
        left, ups = pp
        return f"{move} {left}pp" + (f" ({ups} PP Up{'s' if ups > 1 else ''})" if ups else "")

    # ---- macro actions (controller input only) ----

    def press(self, emu: Emulator, button: str, hold: int = 2, wait: int = 12) -> None:
        """Hold `button`, release, wait `wait` frames, then keep waiting (up to REACT_FRAMES in all) until the screen
        changes: a menu keeps showing its cursor for about 16 frames after it accepted a press."""
        before = self.screen_tiles(emu)
        emu.run([button], hold)
        emu.run([], wait)
        for _ in range(wait, REACT_FRAMES, 4):
            if self.screen_tiles(emu) != before:
                return
            emu.run([], 4)

    def _position(self, emu: Emulator) -> tuple[int, int, int]:
        m = emu.read(self.ram.CUR_MAP, 5)
        return m[0], m[4], m[3]

    def _settle(self, emu: Emulator, limit: int = 60) -> None:
        """Run frames until the walk animation is over."""
        for _ in range(limit):
            if not emu.read(self.ram.WALK_COUNTER, 1)[0]:
                return
            emu.run([], 1)

    def _settle_map(self, emu: Emulator, old_blocks: bytes, limit: int = 160) -> None:
        """After a warp the map id changes ~30 frames before the new map's data loads; wait until it has loaded
        and input is accepted again."""
        for _ in range(0, limit, 4):
            if emu.read(OVERWORLD_MAP, 64) != old_blocks and not emu.read(JOY_IGNORE, 1)[0]:
                break
            emu.run([], 4)
        emu.run([], 8)

    def step(self, emu: Emulator, direction: str) -> str:
        """Move one step. Returns moved | blocked | map_changed | battle | dialog."""
        start = self._position(emu)
        blocks = emu.read(OVERWORLD_MAP, 64)
        for _ in range(24):
            emu.run([direction], 1)
            if self._position(emu) != start or emu.read(self.ram.WALK_COUNTER, 1)[0]:
                break
        self._settle(emu)
        emu.run([], 2)
        now = self._position(emu)
        if now[0] != start[0]:
            self._settle_map(emu, blocks)
        if emu.read(self.ram.IS_IN_BATTLE, 1)[0]:
            return "battle"
        tiles = self.screen_tiles(emu)
        if self.screen_mode(tiles, False)["mode"] != "overworld":
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
            grid = self.grid(emu)
            route = grid.path((px, py), (x, y))
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

    def choose(self, emu: Emulator, label: str, max_moves: int = 12) -> bool:
        """Move the menu cursor up/down to the entry whose text starts with `label`, then press A."""
        for _ in range(max_moves):
            tiles = self.screen_tiles(emu)
            at = tiles.find(CURSOR)
            rows = [r for r, line in enumerate(self.screen_text(tiles)) if line[at % 20 + 1 :].startswith(label)]
            if at < 0 or not rows:
                emu.run([], 8)
                continue
            if rows[0] == at // 20:
                self.press(emu, "a", wait=20)
                return True
            self.press(emu, "down" if rows[0] > at // 20 else "up", wait=8)
        return False

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
        self.press(emu, "start", wait=30)
        if not self.choose(emu, "SAVE"):
            return False
        outcome = self.advance_text(emu)["outcome"]
        for _ in range(2):  # "Would you like to SAVE the game?", then "The older file will be erased to save."
            if outcome != "menu" or not self.choose(emu, "YES"):
                return False
            outcome = self.advance_text(emu)["outcome"]
            if outcome == "closed":
                return True
        return False

    @staticmethod
    def save_complete(sram: bytes) -> bool:
        """Whether cartridge RAM holds a whole in-game save: the right size, a player name and the game's checksum."""
        name = decode(sram[SAVE_MAIN.start : SAVE_MAIN.start + 11])
        if len(sram) != SAVE_SIZE or not name or "·" in name:
            return False
        return ~sum(sram[SAVE_MAIN]) & 0xFF == sram[SAVE_CHECKSUM]

    def advance_text(self, emu: Emulator, max_presses: int = 40, quiet_frames: int = 60) -> dict:
        """Press A through dialog until it closes. Stops, without pressing, at any menu or yes/no prompt.

        Text counts as closed once no text box has been on screen for `quiet_frames` (fades between boxes are
        shorter). A is pressed only when the ▼ prompt shows or the box has stopped changing.
        """
        presses = quiet = stable = 0
        last = None
        for _ in range(max_presses * 40):
            tiles = self.screen_tiles(emu)
            mode = self.screen_mode(tiles, bool(emu.read(self.ram.IS_IN_BATTLE, 1)[0]))
            if mode["cursor"]:
                return {"outcome": "menu", "presses": presses}
            if mode["mode"] == "overworld":
                quiet += 4
                if quiet >= quiet_frames:
                    return {"outcome": "closed" if presses else "no_text", "presses": presses}
                emu.run([], 4)
                continue
            quiet = 0
            body = tiles[: 16 * 20 + 18] + tiles[16 * 20 + 19 :]  # ignore the blinking ▼
            stable = stable + 1 if body == last else 0
            last = body
            if mode["waiting"] or stable >= 6:
                if presses >= max_presses:
                    return {"outcome": "press_limit", "presses": presses}
                self.press(emu, "a", hold=2, wait=6)
                presses += 1
                stable, last = 0, None
            else:
                emu.run([], 4)
        return {"outcome": "timeout", "presses": presses}
