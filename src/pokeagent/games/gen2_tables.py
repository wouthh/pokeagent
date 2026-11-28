"""Find the ROM tables of an English Gen 2 game (Gold, Silver, Crystal and hacks built on their engine) by scanning.

Nothing is located by a per-dump address. The species names are the one run of 251 ten-byte text records, the move and
item names are the 0x50-terminated strings that start with POUND and MASTER BALL, the type names are reached through
the little-endian pointers to NORMAL, FIGHTING and FLYING, the base data is the run of 32-byte records whose first byte
counts 1, 2, 3 and so on (the Pokédex number), the collision permission table is recognised by its first 32 bytes and
the landmark table by its first two entries. A ROM where any of them is missing is not an English Gen 2 game.
"""

import re
from functools import cache

from pokeagent.games.gen1_tables import Where, where

_LETTERS = {chr(65 + i): 0x80 + i for i in range(26)} | {chr(97 + i): 0xA0 + i for i in range(26)}
_CHARS = _LETTERS | {" ": 0x7F, "é": 0xEA, "♂": 0xEF, "♀": 0xF5, ".": 0xE8, "-": 0xE3, "'": 0xE0, "?": 0xE6}
_CHARS |= {str(i): 0xF6 + i for i in range(10)} | {"!": 0xE7, "/": 0xF3, ",": 0xF4, "&": 0xE9}
# the bytes that may appear in a name: the above plus PK/MN, the 'd 'l 'm 'r 's 't 'v glyphs and the word-break dot
_TEXT = bytes(sorted({*_CHARS.values(), 0x54, 0xE1, 0xE2, 0x74, 0x9A, 0x9B, 0x9C, *range(0xD0, 0xD7)}))
_CLASS = bytes(ord("T") if b in _TEXT else ord("P") if b == 0x50 else ord("x") for b in range(256))
_RECORD = "(?:T{10}|" + "|".join(f"T{{{n}}}P{{{10 - n}}}" for n in range(1, 10)) + ")"
SPECIES_COUNT, ITEM_COUNT, MOVE_COUNT, TYPE_COUNT, BASE_SIZE = 251, 249, 251, 0x1C, 32
_SPECIES_RUN = re.compile(f"(?:{_RECORD}){{{SPECIES_COUNT}}}".encode())
# CollisionPermissionTable: 256 bytes, walls at 7 and 0xF, talk-walls (cut trees, counters) at 0x12 and 0x15 and so on
_PERMISSIONS = bytes.fromhex("000000000000000f000000000000000f00001f00001f000000001f00001f0000")
# Landmarks: entry 0 (the "special" landmark) is x, y = 0, 0; entry 1 is New Bark Town at 0x94, 0x74; each ends with the
# name pointer (a bank-local address)
_LANDMARKS = re.compile(rb"\x00\x00.[\x40-\x7f]\x94\x74.[\x40-\x7f]", re.S)


def _enc(text: str) -> bytes:
    return bytes(_CHARS[c] for c in text)


def _strings(rom: bytes, start: int, count: int) -> bool:
    """True when `count` 0x50-terminated strings (1-14 bytes, no control codes: hacks add glyphs) follow `start`."""
    for _ in range(count):
        end = rom.find(b"\x50", start, start + 15)
        if end <= start or min(rom[start:end]) < 0x30:
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


def _base_data(rom: bytes) -> int | None:
    """The 32-byte records of every species, whose first byte is the Pokédex number (1, 2, 3, ...)."""
    for m in re.finditer(rb"\x01", rom):
        at = m.start()
        if rom[at + BASE_SIZE : at + BASE_SIZE + 1] == b"\x02" and rom[at + 2 * BASE_SIZE] == 3:
            ends = at + SPECIES_COUNT * BASE_SIZE
            if ends <= len(rom) and all(rom[at + i * BASE_SIZE] == i + 1 for i in range(SPECIES_COUNT)):
                return at
    return None


@cache
def find_tables(rom: bytes) -> dict[str, Where] | None:
    """Where the ROM tables are (species, items, moves, types, base_data, permissions, landmarks), or None."""
    species = _SPECIES_RUN.search(rom.translate(_CLASS))
    permissions, landmarks = rom.find(_PERMISSIONS), _LANDMARKS.search(rom)
    found = {
        "species": species and species.start(),
        "items": _name_list(rom, "MASTER BALL", ITEM_COUNT),
        "moves": _name_list(rom, "POUND", MOVE_COUNT),
        "types": _types(rom),
        "base_data": _base_data(rom),
        "permissions": permissions if permissions >= 0 else None,
        "landmarks": landmarks and landmarks.start(),
    }
    return None if any(v is None for v in found.values()) else {k: where(v) for k, v in found.items()}
