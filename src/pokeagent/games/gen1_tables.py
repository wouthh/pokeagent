"""Find the ROM tables of an English Gen 1 game (Red, Blue, Yellow and hacks built on them) by scanning the ROM.

Every table is located by facts in the ROM itself, not by a per-dump address: the species names are the one run of
190 ten-byte text records, the move and item names are the 0x50-terminated strings that start with POUND and MASTER
BALL, the type names are reached through the little-endian pointers to NORMAL, FIGHTING and FLYING, and the tile
pair and ledge tables by the shape of their rows. A ROM where any of them is missing is not an English Gen 1 game.
"""

import re
from functools import cache

Where = tuple[int, int]  # (ROM bank, address) as the game's own tables are written

_LETTERS = {chr(65 + i): 0x80 + i for i in range(26)} | {chr(97 + i): 0xA0 + i for i in range(26)}
_CHARS = _LETTERS | {" ": 0x7F, "é": 0xBA, "♂": 0xEF, "♀": 0xF5, ".": 0xE8, "-": 0xE3, "'": 0xE0}
_CHARS |= {str(i): 0xF6 + i for i in range(10)}
# the bytes that may appear in a name: the above plus the PK/MN/?/!/x/./,/() glyphs
_TEXT = bytes(sorted({*_CHARS.values(), 0xE1, 0xE2, 0xE6, 0xE7, 0xF1, 0xF3, 0xF4, 0x9A, 0x9B, 0x9C, 0xBB, 0xBC, 0xBD}))
_CLASS = bytes(ord("T") if b in _TEXT else ord("P") if b == 0x50 else ord("x") for b in range(256))
# ten bytes: 1-10 text bytes padded with 0x50 (KANGASKHAN fills all ten and has no padding)
_RECORD = "(?:T{10}|" + "|".join(f"T{{{n}}}P{{{10 - n}}}" for n in range(1, 10)) + ")"
_SPECIES_RUN = re.compile(f"(?:{_RECORD}){{190}}".encode())
SPECIES_COUNT, ITEM_COUNT, MOVE_COUNT, TYPE_COUNT = 190, 0x61, 165, 0x1B
# ledge rows are (facing, standing tile, ledge tile, joypad bit) where the joypad bit matches the facing, then 0xFF
_LEDGES = re.compile(rb"(?:\x00.{2}\x80|\x04.{2}\x40|\x08.{2}\x20|\x0c.{2}\x10){3,}\xff", re.S)
# tile-pair rows are (tileset, tile, tile) then 0xFF; the table is the longest such run in the home bank
_PAIRS = re.compile(rb"(?:[\x00-\x17].{2}){6,}\xff", re.S)


def _enc(text: str) -> bytes:
    return bytes(_CHARS[c] for c in text)


def _tile_pairs(rom: bytes) -> int | None:
    runs = [m for m in _PAIRS.finditer(rom[:0x4000])]
    return max(runs, key=lambda m: m.end() - m.start()).start() if runs else None


def _ledges(rom: bytes) -> int | None:
    return m.start() if (m := _LEDGES.search(rom)) else None


def where(offset: int) -> Where:
    return (0, offset) if offset < 0x4000 else (offset // 0x4000, 0x4000 + offset % 0x4000)


def _strings(rom: bytes, start: int, count: int) -> bool:
    """True when `count` 0x50-terminated text strings (1-14 bytes each) follow `start`."""
    for _ in range(count):
        end = rom.find(b"\x50", start, start + 15)
        if end <= start or rom[start:end].translate(None, _TEXT):
            return False
        start = end + 1
    return True


def _name_list(rom: bytes, first: str, count: int) -> int | None:
    for m in re.finditer(re.escape(_enc(first) + b"\x50"), rom):
        if _strings(rom, m.start(), count):
            return m.start()
    return None


def _types(rom: bytes) -> int | None:
    names = _enc("NORMAL") + b"\x50" + _enc("FIGHTING") + b"\x50" + _enc("FLYING") + b"\x50"
    for m in re.finditer(re.escape(names), rom):
        bank, addr = where(m.start())
        pointers = b"".join(p.to_bytes(2, "little") for p in (addr, addr + 7, addr + 16))
        start = bank * 0x4000 if bank else 0
        if (at := rom.find(pointers, start, start + 0x4000)) >= 0:
            return at
    return None


@cache
def find_tables(rom: bytes) -> dict[str, Where] | None:
    """Where the ROM tables are (species, items, moves, types, tile_pairs, ledges), or None if one is missing."""
    species = _SPECIES_RUN.search(rom.translate(_CLASS))
    found = {
        "species": species and species.start(),
        "items": _name_list(rom, "MASTER BALL", ITEM_COUNT),
        "moves": _name_list(rom, "POUND", MOVE_COUNT),
        "types": _types(rom),
        "tile_pairs": _tile_pairs(rom),
        "ledges": _ledges(rom),
    }
    return None if any(v is None for v in found.values()) else {k: where(v) for k, v in found.items()}
