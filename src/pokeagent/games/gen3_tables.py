"""The Gen 3 text encoding and the name tables of a Ruby, Sapphire, Emerald, FireRed or LeafGreen ROM, found by
scanning (no game text or ROM offsets are stored here, so a dump that differs from the pret builds still works).

A table is located by the encoded text of its first entry plus the stride between entries, and confirmed by its
second entry. The base stats table has no text, so it is found at a fixed distance from the species names (the games
lay these tables out the same way) and confirmed by its shape.
"""

import re

GAME_CODES = {b"AXVE": "rs", b"AXPE": "rs", b"BPEE": "e", b"BPRE": "frlg", b"BPGE": "frlg"}
ROM_BASE = 0x08000000

CHARMAP = {0x00: " ", 0xAB: "!", 0xAC: "?", 0xAD: ".", 0xAE: "-", 0xAF: "·", 0xB0: "…", 0xB1: "“", 0xB2: "”"}
CHARMAP |= {0xB3: "‘", 0xB4: "’", 0xB5: "♂", 0xB6: "♀", 0xB7: "¥", 0xB8: ",", 0xB9: "×", 0xBA: "/", 0xEF: "▶"}  # noqa: RUF001
CHARMAP |= {0xF0: ":", 0x1B: "é", 0x2D: "&", 0x2E: "+", 0x34: "LV", 0x35: "=", 0x36: ";", 0x53: "PK", 0x54: "MN"}
CHARMAP |= {0x5B: "%", 0x5C: "(", 0x5D: ")", 0x79: "↑", 0x7A: "↓", 0x7B: "←", 0x7C: "→", 0xF1: "Ä", 0xF2: "Ö"}
CHARMAP |= {0xF3: "Ü", 0xF4: "ä", 0xF5: "ö", 0xF6: "ü"}
CHARMAP |= {0xA1 + i: str(i) for i in range(10)}
CHARMAP |= {0xBB + i: chr(ord("A") + i) for i in range(26)} | {0xD5 + i: chr(ord("a") + i) for i in range(26)}
ENCODE = {text: byte for byte, text in CHARMAP.items() if len(text) == 1}
# 0xFC <code> <arguments>: the number of argument bytes of each extended control code (0x09 waits for a button)
CTRL_ARGS = {1: 1, 2: 1, 3: 1, 4: 3, 5: 1, 6: 1, 8: 1, 0xB: 2, 0xC: 1, 0xD: 1, 0xE: 1, 0x10: 2, 0x11: 1}
CTRL_ARGS |= {0x12: 1, 0x13: 1, 0x14: 1}
PARAGRAPH, SCROLL, END = 0xFB, 0xFA, 0xFF


def decode(data: bytes) -> str:
    """Game text to str up to the 0xFF terminator. Paragraph and scroll breaks become newlines; control codes are
    dropped (a 0xFD placeholder shows as {n}); unknown codes become '·'."""
    out, i = [], 0
    while i < len(data) and data[i] != END:
        b = data[i]
        if b in (0xFE, PARAGRAPH, SCROLL):
            out.append("\n")
        elif b == 0xFD:
            out.append(f"{{{data[i + 1]}}}" if i + 1 < len(data) else "")
            i += 1
        elif b == 0xFC:
            i += 1 + CTRL_ARGS.get(data[i + 1] if i + 1 < len(data) else 0, 0)
        elif b in (0xF8, 0xF9):  # a keypad icon or an extra symbol takes one more byte
            out.append("·")
            i += 1
        else:
            out.append(CHARMAP.get(b, "·"))
        i += 1
    return "".join(out)


def encode(text: str) -> bytes:
    return bytes(ENCODE[c] for c in text) + bytes([END])


def find_all(rom: bytes, needle: bytes):
    start = rom.find(needle)
    while start >= 0:
        yield start
        start = rom.find(needle, start + 1)


def find_table(rom: bytes, first: str, second: str, stride: int, index: int = 1) -> int | None:
    """File offset of entry 0 of a table of `stride`-byte text entries whose entry `index` is `first` and the next
    one `second`, or None."""
    for at in find_all(rom, encode(first)):
        base = at - stride * index
        if base >= 0 and rom[base + stride * (index + 1) :].startswith(encode(second)[:-1]):
            return base
    return None


def find_pointer_table(rom: bytes, first: str, second: str, index: int = 0, stride: int = 4, at: int = 0) -> int | None:
    """File offset of entry 0 of a table whose entry `index` holds a pointer to the text `first` and the next entry a
    pointer to `second` (`stride` 4: plain pointers; 8: a pointer at offset `at` of each record)."""
    for text in find_all(rom, encode(first)):
        pointer = (ROM_BASE + text).to_bytes(4, "little")
        for slot in find_all(rom, pointer):
            base = slot - at - stride * index
            nxt = base + at + stride * (index + 1)
            if base >= 0 and int.from_bytes(rom[nxt : nxt + 4], "little") - ROM_BASE >= 0:
                target = int.from_bytes(rom[nxt : nxt + 4], "little") - ROM_BASE
                if rom[target : target + len(second)] == encode(second)[: len(second)]:
                    return base
    return None


# distance from the species name table to the base stats table, by family
STATS_DISTANCE = {"rs": 0x7AAC, "e": 0x7E04, "frlg": 0xE8A4}
COUNTS = {"species": 412, "moves": 355, "types": 18, "abilities": 78, "natures": 25}
ITEM_COUNTS = {"rs": 349, "e": 377, "frlg": 375}


def stats_shape_ok(rom: bytes, base: int) -> bool:
    """Whether `base` looks like the base stats table: 28-byte entries whose pair of types agrees within an evolution
    line (species 1-3, 4-5 and 7-9) and differs between the lines."""
    if base < 0 or base + 28 * 10 > len(rom):
        return False
    pairs = [tuple(rom[base + 28 * i + 6 : base + 28 * i + 8]) for i in range(1, 10)]
    lines = [pairs[0:3], pairs[3:5], pairs[6:9]]
    same = all(len(set(line)) == 1 and all(t < 18 for t in line[0]) for line in lines)
    return same and len({line[0] for line in lines}) == 3


def find_tables(rom: bytes) -> dict | None:
    """Offsets of the name tables of a Gen 3 ROM (family, then the table starts), or None if it is none."""
    family = GAME_CODES.get(rom[0xAC:0xB0])
    if family is None or len(rom) < 0x1000000:
        return None
    species = find_table(rom, "BULBASAUR", "IVYSAUR", 11)
    moves = find_table(rom, "POUND", "KARATE CHOP", 13)
    items = find_table(rom, "MASTER BALL", "ULTRA BALL", 44)
    types = find_table(rom, "NORMAL", "FIGHT", 7, index=0)
    abilities = find_table(rom, "STENCH", "DRIZZLE", 13)
    natures = find_pointer_table(rom, "HARDY", "LONELY")
    if family == "frlg":
        maps = find_pointer_table(rom, "PALLET TOWN", "VIRIDIAN CITY")
    else:  # 8-byte records {x, y, width, height, name*}; section 16 is ROUTE 101
        maps = find_pointer_table(rom, "ROUTE 101", "ROUTE 102", index=16, at=4, stride=8)
    stats = species + STATS_DISTANCE[family] if species is not None else None
    if None in (species, moves, items, types, abilities, maps) or not stats_shape_ok(rom, stats):
        return None
    return {
        "family": family,
        "species": species,
        "moves": moves,
        "items": items,
        "types": types,
        "abilities": abilities,
        "natures": natures,
        "maps": maps,
        "stats": stats,
    }


def names(rom: bytes, base: int, stride: int, count: int) -> list[str]:
    return [decode(rom[base + stride * i : base + stride * (i + 1)]) for i in range(count)]


def pointed_names(rom: bytes, base: int, count: int, stride: int = 4, at: int = 0) -> list[str]:
    """Names read through a pointer table (`stride`-byte records with a pointer at `at`)."""
    out = []
    for i in range(count):
        pointer = int.from_bytes(rom[base + stride * i + at : base + stride * i + at + 4], "little") - ROM_BASE
        out.append(decode(rom[pointer : pointer + 40]) if 0 <= pointer < len(rom) else "")
    return out


RE_GAP = re.compile(r"\s+")


def clean(name: str) -> str:
    """A name from the game as one tidy line (Ruby and Sapphire put layout codes inside some map names)."""
    return RE_GAP.sub(" ", name).strip()
