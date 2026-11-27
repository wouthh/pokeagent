"""Game adapter for the English Pokémon Red and Blue (Game Boy).

Addresses come from the pret/pokered disassembly; every address used here is identical in the English Red and Blue
builds. Pokémon, item and move names are read from the user's ROM at runtime, so no game text is stored in this
repository. All game progress goes through controller input; RAM is only read.
"""

import hashlib
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from pokeagent.emulator.client import Emulator

ROMS = {
    "ea9bcae617fdf159b045185467ae58b2e4a48b9a": "Pokémon Red (English)",
    "d7037c83e1ae5b39bde3c30787637ba1d4c48ce2": "Pokémon Blue (English)",
}
FAMILY = "pokemon-gen1"
CORE = "sameboy"
CORE_OPTIONS = {"sameboy_model": "Game Boy"}
HALL_OF_FAME = 0x76
DEFAULT_OBJECTIVE = "Finish the game: defeat the Elite Four and the Champion and enter the Hall of Fame."
DEFAULT_CHECK = {"obs": "map_id", "op": "==", "value": HALL_OF_FAME}
MAP_NAMES = Path(__file__).with_name("gen1_map_names.txt").read_text().splitlines()

# WRAM / HRAM (pret pokered symbols)
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
# ROM tables (bank, address)
MONSTER_NAMES, ITEM_NAMES, MOVE_NAMES = (0x07, 0x421E), (0x01, 0x472B), (0x2C, 0x4000)
TILE_PAIR_COLLISIONS_LAND, LEDGE_TILES = (0x00, 0x0C7E), (0x06, 0x66CF)

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


class Gen1:
    """Reads state and drives inputs for one English Red/Blue ROM inside an `Emulator`."""

    def __init__(self, rom: bytes):
        sha1 = hashlib.sha1(rom).hexdigest()
        if sha1 not in ROMS:
            raise ValueError(f"unsupported ROM (sha1 {sha1}); supported: {ROMS}")
        self.title = ROMS[sha1]
        self.rom = rom
        self.species = [decode(self._rom(MONSTER_NAMES, 190 * 10)[i * 10 : i * 10 + 10]) for i in range(190)]
        self.items = self._names(ITEM_NAMES, 0x61)
        self.moves = self._names(MOVE_NAMES, 165)
        pairs = self._table(TILE_PAIR_COLLISIONS_LAND, 3)
        self.tile_pairs = {}
        for tileset, a, b in pairs:
            self.tile_pairs.setdefault(tileset, set()).update({(a, b), (b, a)})
        self.ledges = {
            (FACING_CODES[f], standing, ledge): FACING_CODES[f] for f, standing, ledge, _ in self._table(LEDGE_TILES, 4)
        }

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
        m = emu.read(CUR_MAP, 12)
        map_id, y, x = m[0], m[3], m[4]
        tiles = self.screen_tiles(emu)
        battle_kind = emu.read(IS_IN_BATTLE, 1)[0]
        facing = FACING_CODES.get(emu.read(SPRITES1 + 9, 1)[0], "?")
        party = []
        count = min(emu.read(PARTY_COUNT, 1)[0], 6)
        mons, nicks = emu.read(PARTY_MONS, 44 * 6), emu.read(PARTY_NICKS, 11 * 6)
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
                    "moves": [self.move_name(mv) for mv in mon[8:12] if mv],
                    "exp": int.from_bytes(mon[14:17]),
                }
            )
        bag = []
        raw = emu.read(BAG_ITEMS, 2 * 20)
        for i in range(min(emu.read(NUM_BAG_ITEMS, 1)[0], 20)):
            bag.append({"item": self.item_name(raw[2 * i]), "qty": raw[2 * i + 1]})
        battle = None
        if battle_kind:
            e = emu.read(ENEMY_SPECIES, 16)
            battle = {
                "kind": {1: "wild", 2: "trainer"}.get(battle_kind, "lost"),
                "enemy": self.species_name(e[0]),
                "enemy_level": emu.read(ENEMY_LEVEL, 1)[0],
                "enemy_hp": int.from_bytes(e[1:3]),
                "enemy_max_hp": int.from_bytes(emu.read(ENEMY_MAX_HP, 2)),
            }
        badges = emu.read(BADGES, 1)[0]
        player_name = decode(emu.read(PLAYER_NAME, 11))
        return {
            "map_id": map_id,
            "map": MAP_NAMES[map_id] if map_id < len(MAP_NAMES) else f"map {map_id:#04x}",
            "x": x,
            "y": y,
            "facing": facing,
            "map_size": [m[11] * 2, m[10] * 2],
            "screen": self.screen_mode(tiles, bool(battle_kind)),
            "screen_text": [line for line in self.screen_text(tiles) if line.strip()],
            "player_name": player_name,
            "rival_name": decode(emu.read(RIVAL_NAME, 11)),
            "party": party,
            "bag": bag,
            "money": bcd(emu.read(MONEY, 3)),
            "badges": badges.bit_count(),
            "badge_bits": badges,
            "dex_owned": sum(b.bit_count() for b in emu.read(DEX_OWNED, 19)),
            "dex_seen": sum(b.bit_count() for b in emu.read(DEX_SEEN, 19)),
            "battle": battle,
            # Before a game is started or loaded, WRAM holds power-on noise (SameBoy randomises it) or zeros.
            "in_game": bool(m[10] and m[11]) and bool(player_name) and "·" not in player_name,
        }

    def grid(self, emu: Emulator) -> Grid:
        m = emu.read(CUR_MAP, 12)
        tileset, height, width = m[9], m[10], m[11]
        stride = width + 6
        blocks = emu.read(OVERWORLD_MAP, stride * (height + 6))
        t = emu.read(TILESET_BANK, 7)
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
        for i in range(1, 16):
            if s1[i * 16] and s1[i * 16 + 2] != 0xFF:
                sprites.add((s2[i * 16 + 5] - 4, s2[i * 16 + 4] - 4))
        n = min(emu.read(NUM_WARPS, 1)[0], 32)
        raw = emu.read(WARPS, 4 * n) if n else b""
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
            emu.read(GRASS_TILE, 1)[0],
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
        return {
            "map_id": state["map_id"],
            "x": state["x"],
            "y": state["y"],
            "badges": state["badges"],
            "party_count": len(state["party"]),
            "max_level": max((p["level"] for p in state["party"]), default=0),
            "dex_owned": state["dex_owned"],
            "money": state["money"],
            "in_battle": state["battle"] is not None,
            "in_game": state["in_game"],
        }

    @staticmethod
    def state_text(state: dict) -> str:
        """The state as a few lines for the model."""
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
        lines = [
            f"Map: {state['map']} (id {state['map_id']:#04x}, {width}x{height} steps); you are at "
            f"({state['x']},{state['y']}) facing {state['facing']}. Screen: {screen['mode']}"
            + (f" ({', '.join(flags)})" if flags else "")
            + (f", cursor at column {screen['cursor_at'][0]} row {screen['cursor_at'][1]}" if screen["cursor"] else ""),
            f"Player {state['player_name']}, rival {state['rival_name']}. Money {state['money']}, badges "
            f"{state['badges']}/8, Pokédex owned {state['dex_owned']} seen {state['dex_seen']}.",
        ]
        party = [
            f"{p['name']} ({p['species']}) L{p['level']} HP {p['hp']}/{p['max_hp']} {p['status']} "
            f"[{', '.join(p['moves'])}]"
            for p in state["party"]
        ]
        lines.append("Party: " + ("; ".join(party) if party else "none"))
        lines.append("Bag: " + (", ".join(f"{b['item']} x{b['qty']}" for b in state["bag"]) or "empty"))
        if battle := state["battle"]:
            lines.append(
                f"Battle ({battle['kind']}): {battle['enemy']} L{battle['enemy_level']} "
                f"HP {battle['enemy_hp']}/{battle['enemy_max_hp']}"
            )
        if state["screen_text"]:
            lines.append("Screen text:\n" + "\n".join("  " + line for line in state["screen_text"]))
        return "\n".join(lines)

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
        m = emu.read(CUR_MAP, 5)
        return m[0], m[4], m[3]

    def _settle(self, emu: Emulator, limit: int = 60) -> None:
        """Run frames until the walk animation is over."""
        for _ in range(limit):
            if not emu.read(WALK_COUNTER, 1)[0]:
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
            if self._position(emu) != start or emu.read(WALK_COUNTER, 1)[0]:
                break
        self._settle(emu)
        emu.run([], 2)
        now = self._position(emu)
        if now[0] != start[0]:
            self._settle_map(emu, blocks)
        if emu.read(IS_IN_BATTLE, 1)[0]:
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

    def advance_text(self, emu: Emulator, max_presses: int = 40, quiet_frames: int = 60) -> dict:
        """Press A through dialog until it closes. Stops, without pressing, at any menu or yes/no prompt.

        Text counts as closed once no text box has been on screen for `quiet_frames` (fades between boxes are
        shorter). A is pressed only when the ▼ prompt shows or the box has stopped changing.
        """
        presses = quiet = stable = 0
        last = None
        for _ in range(max_presses * 40):
            tiles = self.screen_tiles(emu)
            mode = self.screen_mode(tiles, bool(emu.read(IS_IN_BATTLE, 1)[0]))
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
