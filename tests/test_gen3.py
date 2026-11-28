"""Gen 3 adapter (Ruby, Sapphire, Emerald, FireRed, LeafGreen). The first group needs no ROM: it builds and encrypts
Pokémon, saves and map grids from synthetic bytes. The ROM groups use dumps in the main checkout's ignored roms/ folder
and skip without them."""

import hashlib
import struct

import pytest

from pokeagent.emulator.client import Emulator
from pokeagent.emulator.cores import core_path
from pokeagent.games import gen3_tables as tables
from pokeagent.games import identify
from pokeagent.games.pokemon_gen1 import Gen1
from pokeagent.games.pokemon_gen3 import (
    LAYOUTS,
    SECTOR,
    SECTOR_SIGNATURE,
    SUBSTRUCTS,
    Gen3,
    Grid,
    checksum,
    decode_mon,
    decode_money,
    decode_quantity,
    exit_warps,
    flag,
    layout_problems,
    load_map_names,
    valid_name,
    words,
)

ROM_NAMES = {
    "ruby": "Pokemon Ruby.gba",
    "sapphire": "Pokemon Sapphire.gba",
    "emerald": "Pokemon Emerald.gba",
    "firered": "Pokemon Fire Red.gba",
    "leafgreen": "Pokemon Leaf Green.gba",
}
TABLE_OFFSETS = {  # the pret symbol offsets, except the Sapphire dump, which differs from every pret build
    "ruby": {"species": 0x1F7184, "moves": 0x1F8338, "items": 0x3C5580, "stats": 0x1FEC30},
    "sapphire": {"species": 0x1F70F4, "moves": 0x1F82A8, "items": 0x3C55BC, "stats": 0x1FEBA0},
    "emerald": {"species": 0x3185C8, "moves": 0x31977C, "items": 0x5839A0, "stats": 0x3203CC},
    "firered": {"species": 0x245F50, "moves": 0x247104, "items": 0x3DB098, "stats": 0x2547F4},
    "leafgreen": {"species": 0x245F2C, "moves": 0x2470E0, "items": 0x3DAED4, "stats": 0x2547D0},
}


def make_mon(pid=0x12345678, oid=0x0BADF00D, species=7, level=5, moves=(33, 39, 0, 0), good_checksum=True):
    """A `struct Pokemon` built the way the game stores it: data blocks in the order set by pid % 24, XORed with
    pid ^ oid, plus a checksum and the plain battle stats."""
    growth = struct.pack("<HHIBBH", species, 0, 135, 0b01, 70, 0)
    attacks = struct.pack("<4H4B", *moves, 35, 30, 0, 0)
    evs = bytes([1, 2, 3, 4, 5, 6, 0, 0, 0, 0, 0, 0])
    word = (31) | (30 << 5) | (29 << 10) | (28 << 15) | (27 << 20) | (26 << 25) | (1 << 31)
    misc = struct.pack("<BBHII", 0, 88, 5, word, 0)
    blocks = {"G": growth, "A": attacks, "E": evs, "M": misc}
    data = b"".join(blocks[c] for c in SUBSTRUCTS[pid % 24])
    total = sum(struct.unpack("<24H", data)) & 0xFFFF
    secure = b"".join(struct.pack("<I", w ^ pid ^ oid) for w in struct.unpack("<12I", data))
    head = struct.pack("<II", pid, oid) + bytes([0xBB, 0xBC]) + bytes(8) + bytes([2, 2]) + bytes(7) + bytes(1)
    head += struct.pack("<HH", total if good_checksum else total ^ 1, 0)
    tail = struct.pack("<IBBHHHHHHH", 0x08, level, 0, 19, 20, 11, 12, 8, 10, 11)
    return head + secure + tail


def test_charmap_text_round_trips_and_stops_at_the_terminator():
    assert tables.decode(tables.encode("Hello, World!")) == "Hello, World!"
    assert tables.decode(tables.encode("AB") + b"\xff" + tables.encode("CD")) == "AB"
    # a paragraph break, a colour code with its argument, a placeholder and a newline
    assert tables.decode(b"\xbb\xfb\xfc\x01\x04\xbc\xfd\x01\xfe\xbd\xff") == "A\nB{1}\nC"
    assert tables.decode(b"\x01\xff") == "·"  # unknown bytes show as a dot


def test_names_are_valid_only_when_terminated():
    assert valid_name(tables.encode("AAAAAAA")[:8]) and valid_name(tables.encode("ASH") + bytes(4))
    assert valid_name(tables.encode("ASH K") + b"\xff") and not valid_name(b"\x00\xbb\xff")
    assert (
        not valid_name(bytes(8)) and not valid_name(b"\xff" * 8) and not valid_name(b"\xbb\xbb\xbb\x00\x00\x00\x00\x00")
    )


@pytest.mark.parametrize("pid", [0, 1, 23, 0x12345678, 0xFFFFFFFF])
def test_decode_mon_decrypts_every_block_order(pid):
    mon = decode_mon(make_mon(pid=pid))
    assert mon["species"] == 7 and mon["exp"] == 135 and mon["moves"] == [33, 39, 0, 0] and mon["pp"] == [35, 30, 0, 0]
    assert mon["evs"] == [1, 2, 3, 4, 5, 6] and mon["ivs"] == [31, 30, 29, 28, 27, 26] and mon["ability_slot"] == 1
    assert (mon["level"], mon["hp"], mon["max_hp"], mon["stats"]) == (5, 19, 20, [11, 12, 8, 10, 11])
    assert mon["friendship"] == 70 and mon["pp_bonuses"] == 1


def test_decode_mon_rejects_a_bad_checksum_and_short_data():
    assert decode_mon(make_mon(good_checksum=False)) is None
    assert decode_mon(b"\0" * 79) is None
    assert decode_mon(make_mon()[:80])["species"] == 7  # a box Pokémon has no battle stats


def test_money_and_quantities_are_xored_with_the_key_flags_are_bits():
    key = 0xDEADBEEF
    assert decode_money(3000 ^ key, key) == 3000 and decode_money(3000, 0) == 3000
    assert decode_quantity(12 ^ (key & 0xFFFF), key) == 12
    flags = bytes([0b00000100, 0, 0b10000000])
    assert flag(flags, 2, 0) and not flag(flags, 3, 0) and flag(flags, 23, 0) and not flag(flags, 99, 0)
    assert flag(flags, 0x822, 0x820) and not flag(flags, 0x81F, 0x820)  # a base offsets the flag number


def test_layout_probe_accepts_zeros_and_real_pointers_and_rejects_moved_ram():
    layout = LAYOUTS["e"]

    def memory(callback, sb1=0x02025A20, sb2=0x02024A74):
        ram = {layout.MAIN + 4: callback, layout.SB1: sb1, layout.SB2: sb2}
        return lambda addr, n: struct.pack("<I", ram.get(addr, 0))[:n]

    assert layout_problems(memory(0), layout) == []  # not started yet
    assert layout_problems(memory(0x08085E5D), layout) == []
    assert layout_problems(memory(0x08085E5D, sb1=0, sb2=0), layout) == []
    assert layout_problems(memory(0xDFE6EAF6), layout) == ["main callback"]
    assert layout_problems(memory(0x08085E5D, sb1=0x03005D8C), layout) == ["save block 1 pointer"]
    fixed = LAYOUTS["rs"]  # Ruby and Sapphire keep their save blocks at fixed addresses
    assert layout_problems(lambda addr, n: bytes(n), fixed) == []


def synthetic_grid(rows: list[str], surfing=False) -> Grid:
    """A grid from text: '#' blocked, '.' floor, ~ water, v < > ^ ledges, plus a map border of blocked cells."""
    height, width = len(rows), len(rows[0])
    stride = width + 15
    tiles, behaviours = [0x400] * (stride * (height + 14)), [0] * (stride * (height + 14))
    for y, row in enumerate(rows):
        for x, c in enumerate(row):
            i = (y + 7) * stride + x + 7
            tiles[i] = 0x400 if c == "#" else 0
            behaviours[i] = {"D": 0x65, "~": 0x15, "v": 0x3B, "^": 0x3A, "<": 0x39, ">": 0x38}.get(c, 0)
            if c in "v^<>":
                tiles[i] = 0x400  # ledge tiles are blocked from every other side
    return Grid(width, height, tiles, behaviours, surfing)


def test_grid_ledges_are_one_way_and_hop_two_tiles():
    grid = synthetic_grid(["...", ".v.", "..."])
    assert grid.path((1, 0), (1, 2)) == ["down"]  # hopped: the ledge tile itself is never stood on
    assert grid.path((1, 2), (1, 0)) == ["left", "up", "up", "right"] or len(grid.path((1, 2), (1, 0))) == 4
    assert "v" in grid.render((0, 0))
    wall = synthetic_grid(["#v#", "...", "#.#"])
    assert wall.path((1, 2), (1, 0)) is None  # no way up through a south-facing ledge


def test_grid_water_needs_surf_sprites_block_and_warps_open_walls():
    sea = ["...~~~..."]
    assert synthetic_grid(sea).path((0, 0), (8, 0)) is None
    assert len(synthetic_grid(sea, surfing=True).path((0, 0), (8, 0))) == 8
    grid = synthetic_grid(["...", "###", "..."])
    assert grid.path((0, 0), (0, 2)) is None
    grid.warps = {(1, 1)}
    assert grid.path((0, 0), (0, 2)) is not None
    grid.sprites = {(1, 1)}
    assert grid.path((0, 0), (0, 2)) is None
    assert "S" in grid.render((0, 0)) and "~" in synthetic_grid(sea).render((0, 0))


def synthetic_rom(species_at: int) -> bytearray:
    rom = bytearray(0x1000000)
    rom[0xAC:0xB0] = b"BPEE"
    for i, name in enumerate(("BULBASAUR", "IVYSAUR")):
        rom[species_at + 11 * (i + 1) : species_at + 11 * (i + 2)] = tables.encode(name).ljust(11, b"\xff")
    return rom


def test_table_scanner_finds_tables_by_their_first_two_entries():
    rom = synthetic_rom(0x2000)
    assert tables.find_table(bytes(rom), "BULBASAUR", "IVYSAUR", 11) == 0x2000
    assert tables.find_table(bytes(rom), "BULBASAUR", "VENUSAUR", 11) is None
    rom[0x5000 : 0x5000 + 10] = tables.encode("BULBASAUR")  # a stray copy of the first name is not a table
    assert tables.find_table(bytes(rom), "BULBASAUR", "IVYSAUR", 11) == 0x2000
    assert tables.find_tables(bytes(rom)) is None  # the other tables are missing
    assert not Gen3.recognises(bytes(rom)) and not Gen3.recognises(b"not a rom")
    with pytest.raises(ValueError, match="unsupported ROM"):
        Gen3(bytes(rom))


def test_stats_table_shape():
    rom = bytearray(28 * 10)
    for species, types in {
        1: (12, 3),
        2: (12, 3),
        3: (12, 3),
        4: (10, 10),
        5: (10, 10),
        7: (11, 11),
        8: (11, 11),
        9: (11, 11),
    }.items():
        rom[28 * species + 6 : 28 * species + 8] = bytes(types)
    assert tables.stats_shape_ok(bytes(rom), 0)
    rom[28 * 3 + 6] = 1
    assert not tables.stats_shape_ok(bytes(rom), 0) and not tables.stats_shape_ok(bytes(rom), 50)


def build_save(family: str, counter=1, ids=range(14), good=True) -> bytes:
    """A 128 KiB flash image: one save slot of 14 sectors with signatures and checksums, the other slot empty."""
    from pokeagent.games.pokemon_gen3 import _sector_sizes

    sizes = _sector_sizes(family)
    flash = bytearray(b"\xff" * 0x20000)
    for position, sid in enumerate(ids):
        data = bytes((position + i) % 251 for i in range(sizes[sid])).ljust(3968, b"\0")
        footer = struct.pack("<HHII", sid, checksum(data[: sizes[sid]]) ^ (0 if good else 1), SECTOR_SIGNATURE, counter)
        flash[position * SECTOR : (position + 1) * SECTOR] = data + bytes(116) + footer
    return bytes(flash)


class FakeRom(Gen3):
    def __init__(self, family):  # a save check needs only the family
        self.family = family


@pytest.mark.parametrize("family", ["rs", "e", "frlg"])
def test_save_complete_checks_sectors_signature_ids_and_checksums(family):
    adapter = FakeRom(family)
    assert adapter.save_complete(build_save(family))
    assert adapter.save_complete(build_save(family, ids=list(range(5, 14)) + list(range(5))))  # rotated sectors
    assert not adapter.save_complete(build_save(family, good=False))
    assert not adapter.save_complete(build_save(family, ids=[0, 0, *range(2, 14)]))
    assert not adapter.save_complete(b"\xff" * 0x20000) and not adapter.save_complete(b"short")


def test_the_state_text_names_what_a_player_needs():
    mon = decode_mon(make_mon())
    state = {
        "map_id": 0x0403, "map": "PALLET TOWN", "x": 5, "y": 6, "facing": "up", "map_size": [24, 20],
        "screen": {"mode": "overworld", "cursor": False, "cursor_at": None, "yes_no": False, "waiting": False},
        "screen_text": [], "player_name": "ASH", "rival_name": "GARY", "money": 3000, "badges": 1, "dex_owned": 2,
        "dex_seen": 4, "battle": None, "hall_of_fame": False, "in_game": True, "layout_problems": [],
        "bag": [{"item": "POTION", "qty": 3}],
        "party": [{
            "species": "SQUIRTLE", "name": "SHELL", "level": mon["level"], "hp": 19, "max_hp": 20, "status": "ok",
            "types": ["WATER"], "moves": ["TACKLE"], "pp": [(35, 1)], "exp": mon["exp"], "stats": mon["stats"],
            "evs": mon["evs"], "ivs": mon["ivs"], "nature": "ADAMANT", "nature_up": "Atk", "nature_down": "SpA",
            "ability": "TORRENT", "item": "", "friendship": 70,
        }],
    }  # fmt: skip
    text = Gen3.state_text(state, detail=True)
    assert "SHELL (SQUIRTLE, WATER) L5 HP 19/20 ok ADAMANT, TORRENT [TACKLE 35pp (1 PP Up)]" in text
    assert "EVs HP 1 Atk 2" in text and "IVs HP 31 Atk 30" in text and "ADAMANT: +Atk -SpA" in text
    assert "POTION x3" in text and "Money 3000" in text
    assert Gen3.fingerprint(state)["position"] == (0x0403, 5, 6)
    assert Gen3.progress_score(state, 3, 10) > Gen3.progress_score(state | {"badges": 0}, 3, 10)
    assert Gen3.observations(state)["hall_of_fame"] is False
    moved = state | {"layout_problems": ["main callback"]}
    assert "memory layout does not match" in Gen3.state_text(moved)


# ---- on the user's own ROMs ----


@pytest.fixture(scope="session")
def gba(find_rom):
    """gba(game) -> (adapter, ROM path); skips when that ROM is not in roms/."""

    def load(game: str):
        path = find_rom(ROM_NAMES[game])
        return identify(path.read_bytes()), path

    return load


@pytest.mark.parametrize("game", ROM_NAMES)
def test_every_gen3_rom_is_identified_by_its_tables(gba, game):
    adapter, path = gba(game)
    assert isinstance(adapter, Gen3) and adapter.title in adapter.ROMS.values()
    assert hashlib.sha1(path.read_bytes()).hexdigest() in adapter.ROMS
    for name, offset in TABLE_OFFSETS[game].items():
        assert adapter.found[name] == offset, name
    assert len(adapter.species) == 412 and len(adapter.moves) == 355 and len(adapter.types) == 18
    assert adapter.species_name(1) == "BULBASAUR" and adapter.move_name(1) == "POUND" and adapter.types[11] == "WATER"
    assert adapter.item_name(13) == "POTION" and adapter.natures[:2] == ["HARDY", "LONELY"]
    assert adapter.section_name(adapter.first_section + (0 if adapter.family == "frlg" else 16)) in (
        "PALLET TOWN",
        "ROUTE 101",
    )
    assert not Gen1.recognises(path.read_bytes())


def test_the_starters_differ_by_family(gba):
    hoenn, _ = gba("emerald")
    kanto, _ = gba("firered")
    assert {hoenn.species_name(i) for i in hoenn.STARTERS} == {"TREECKO", "TORCHIC", "MUDKIP"}
    assert {kanto.species_name(i) for i in kanto.STARTERS} == {"BULBASAUR", "CHARMANDER", "SQUIRTLE"}


def test_hacks_with_moved_names_are_not_gen_3_games(find_rom):
    for name in ("03 Pokemon_ROWE.gba", "04 Pokemon Radical Red 3.0.2.gba"):
        assert not Gen3.recognises(find_rom(name).read_bytes())  # the generic adapter takes them (test_generic.py)


@pytest.fixture(scope="module", params=ROM_NAMES)
def booted(request, gba):
    adapter, path = gba(request.param)
    with Emulator(core_path(adapter.CORE), path, options=adapter.CORE_OPTIONS) as emu:
        emu.run([], 600)
        yield request.param, adapter, emu


def test_the_worker_reads_ewram_and_iwram_but_never_the_rom(booted):
    _, adapter, emu = booted
    assert emu.info["library"] == "mGBA" and (emu.info["width"], emu.info["height"]) == (240, 160)
    assert len(emu.read(0x02000000, 0x10000)) == 0x10000 and len(emu.read(0x03007000, 0x1000)) == 0x1000
    for address in (0x08000000, 0x00000000):  # ROM and BIOS
        with pytest.raises(Exception, match="not readable"):
            emu.read(address, 4)
    assert emu.read(0x02000000, 4)  # and the adapter's own reads work on the same memory map
    assert adapter.RAM_RANGES == ((0x02000000, 0x40000), (0x03000000, 0x8000))


def test_ram_hash_covers_all_of_ewram_and_iwram_and_the_save(booted):
    _, _, emu = booted
    ewram = b"".join(emu.read(0x02000000 + o, 0x10000) for o in range(0, 0x40000, 0x10000))
    iwram = emu.read(0x03000000, 0x8000)
    assert emu.ram_hash() == hashlib.sha256(ewram + iwram + emu.save_ram()).hexdigest()
    assert len(emu.save_ram()) == 0x20000


def test_a_fresh_game_has_no_game_in_progress_and_the_shared_state_shape(booted):
    _, adapter, emu = booted
    state = adapter.state(emu)
    assert state["in_game"] is False and state["layout_problems"] == []
    assert set(state) >= {
        "map_id",
        "map",
        "x",
        "y",
        "facing",
        "map_size",
        "screen",
        "screen_text",
        "party",
        "bag",
        "money",
    }
    assert set(state) >= {
        "badges",
        "dex_owned",
        "dex_seen",
        "battle",
        "in_game",
        "player_name",
        "rival_name",
        "layout_unverified",
    }
    assert set(state["screen"]) == {"mode", "cursor", "cursor_at", "yes_no", "waiting"}
    assert "No game is in progress" in adapter.state_text(state)
    assert emu.save_state() and adapter.advance_text(emu, quiet_frames=8)["outcome"] in ("no_text", "closed", "timeout")


class StubEmu:
    """RAM as a dict of byte runs; everything else reads as zeros."""

    def __init__(self, **runs):
        self.runs = runs

    def read(self, addr, n):
        for start, data in self.runs.items():
            if int(start[1:], 16) <= addr < int(start[1:], 16) + len(data):
                return (data + bytes(n))[addr - int(start[1:], 16) :][:n]
        return bytes(n)


def stub_adapter(family="e"):
    adapter = FakeRom(family)
    adapter.rom, adapter.layout = bytes(0x100), LAYOUTS[family]
    adapter.bad_layout, adapter.functions, adapter._behaviours = [], None, {}
    adapter.sections, adapter.first_section = ["LITTLEROOT TOWN"] * 20, 0
    adapter.map_names = load_map_names(family)
    return adapter


def test_no_game_is_in_progress_until_a_map_is_loaded_and_the_grid_never_raises():
    adapter = stub_adapter()
    sb1, sb2 = 0x02025A20, 0x02024A74
    emu = StubEmu(
        **{
            f"x{LAYOUTS['e'].SB1:x}": struct.pack("<II", sb1, sb2),
            f"x{sb2:x}": tables.encode("ASH")[:8].ljust(8, b"\xff"),  # named, but the map header is still empty
        }
    )
    state = adapter.state(emu)
    assert state["in_game"] is False and state["map"] == ""
    grid = adapter.grid(emu)  # used to raise IndexError on the empty header
    assert (grid.width, grid.height) == (0, 0) and grid.path((0, 0), (1, 0)) is None
    header = struct.pack("<II", 0x08000010, 0x08000020) + bytes(20)  # a header in the ROM, but a grid of size zero
    assert not adapter._map_loaded(header, (0, 0, 0x02030000))
    assert adapter._map_loaded(header, (20, 20, 0x02030000))
    assert not adapter._map_loaded(struct.pack("<II", 0x08000010, 0x09999999) + bytes(20), (20, 20, 0x02030000))
    assert adapter._tileset_behaviours(0x08000010) == []  # tileset pointers outside the ROM give no behaviours


def test_map_identifiers_name_rooms_and_fall_back_to_the_section_and_group_number():
    for family, size in (("rs", 394), ("e", 518), ("frlg", 425)):
        names = load_map_names(family)
        assert len(names) == size and all(" " not in name for name in names.values())
    assert words("LittlerootTown_BrendansHouse_1F") == "Littleroot Town - Brendans House - 1F"
    assert words("Route101") == "Route 101" and words("SSAnne_Exterior") == "SS Anne - Exterior"
    adapter = stub_adapter()
    assert adapter.map_name(1, 0, 0) == "Littleroot Town - Brendans House - 1F" or adapter.map_name(1, 0, 0).startswith(
        "LITTLEROOT"
    )
    adapter.sections = ["PETALBURG CITY"] * 20  # a header whose section does not fit the identifier: do not guess
    assert adapter.map_name(0, 9, 0) == "PETALBURG CITY (0.9)"
    assert adapter.map_name(99, 99, 0) == "PETALBURG CITY (99.99)"  # a map pret does not list


@pytest.mark.parametrize(("game", "where", "section", "name"), [
    ("ruby", (1, 0), 0, "Littleroot Town - Brendans House - 1F"),
    ("emerald", (0, 9), 0, "Littleroot Town"),
    ("firered", (4, 1), 0x58, "Pallet Town - Players House - 2F"),
    ("leafgreen", (3, 0), 0x58, "Pallet Town"),
])  # fmt: skip
def test_rooms_have_their_own_names_on_the_real_roms(gba, game, where, section, name):
    adapter, _ = gba(game)
    assert adapter.map_name(*where, section) == name


def test_rom_pointers_reach_the_second_half_of_an_expanded_dump():
    adapter = stub_adapter()
    adapter.rom = bytes(0x2000000)  # a 32 MiB dump: pointers start with 0x09 past 16 MiB
    assert adapter._rom_at(0x09000100, 0x18) == 0x1000100
    assert adapter._rom_at(0x0A000000) is None and adapter._rom_at(0x09FFFFFF, 2) is None


class ScreenEmu:
    """An emulator whose picture is a function of the frame number and the buttons pressed so far."""

    def __init__(self, fading_until=0, reacts_at=None):
        self.frame_no, self.pressed, self.fading_until, self.reacts_at = 0, [], fading_until, reacts_at
        self.reacted = False

    def run(self, buttons=(), frames=1):
        for _ in range(frames):
            self.frame_no += 1
            if buttons and self.frame_no >= self.fading_until:  # input during the fade is ignored
                self.pressed.append(self.frame_no)
                self.reacted = True

    def read(self, addr, n):
        return bytes(n)

    def frame(self):
        picture = self.frame_no if self.frame_no < self.fading_until else -1  # a fade changes every frame
        picture = (picture, self.reacted and self.frame_no >= self.reacts_at if self.reacts_at else self.reacted)
        return type("Image", (), {"tobytes": lambda self_: repr(picture).encode()})()


def test_press_waits_for_a_fade_to_end_and_for_the_screen_to_settle():
    adapter = stub_adapter()
    emu = ScreenEmu(fading_until=30)
    adapter.press(emu, "a")
    assert emu.pressed and emu.pressed[0] >= 30  # nothing was pressed while the picture changed
    assert emu.frame_no >= emu.pressed[-1] + 6  # and it ran on until the screen settled
    quick = ScreenEmu()
    adapter.press(quick, "a")
    assert len(quick.pressed) == 6 and quick.frame_no < 60  # a settled screen: pressed at once, held 6 frames
    never = ScreenEmu(fading_until=10**6)  # an animated screen: capped, still pressed
    adapter.press(never, "a")
    assert never.frame_no < 200


def test_tap_ignores_the_screen_and_ruby_reads_both_message_windows():
    adapter = stub_adapter()
    emu = ScreenEmu(fading_until=10**6)
    adapter.tap(emu, "b", hold=2, wait=8)
    assert emu.frame_no == 10 and len(emu.pressed) == 0
    rs = stub_adapter("rs")
    window = bytearray(0x30)
    struct.pack_into("<H", window, 0x16, 3)  # printing
    struct.pack_into("<I", window, 0x20, 0x08000100)
    waiting = bytearray(window)
    struct.pack_into("<H", waiting, 0x16, 8)  # waiting for A at a paragraph's end
    layout = LAYOUTS["rs"]
    emu = StubEmu(**{f"x{layout.FIELD_WINDOW:x}": bytes(0x30), f"x{layout.MENU_WINDOW:x}": bytes(waiting)})
    assert rs._printers(emu) == [(1, 0x08000100, 2)]  # the intro's text lives in the menu window
    emu = StubEmu(**{f"x{layout.FIELD_WINDOW:x}": bytes(window), f"x{layout.MENU_WINDOW:x}": bytes(0x30)})
    assert rs._printers(emu) == [(0, 0x08000100, 0)]


def test_only_door_tiles_count_as_exits():
    grid = synthetic_grid([".D.."])
    assert exit_warps(grid, {(0, 0), (1, 0), (2, 0)}) == {(1, 0)}  # the mat beside a door is bookkeeping, not an exit
    grid.warps = exit_warps(grid, {(0, 0), (1, 0)})
    assert "W" in grid.render((0, 0)) and grid.render((0, 0)).count("W") == 1
    assert grid.path((0, 0), (3, 0)) is not None


def test_ruby_text_is_read_at_the_windows_index_not_at_its_start():
    rs = stub_adapter("rs")
    window = bytearray(0x30)
    struct.pack_into("<H", window, 0x16, 3)
    struct.pack_into("<H", window, 0x1E, 26)
    struct.pack_into("<I", window, 0x20, 0x08000100)
    layout = LAYOUTS["rs"]
    emu = StubEmu(**{f"x{layout.FIELD_WINDOW:x}": bytes(window), f"x{layout.MENU_WINDOW:x}": bytes(0x30)})
    assert rs._printers(emu) == [(0, 0x08000100 + 26, 0)]


def test_ruby_yes_no_prompt_reports_the_field_question_not_the_menu_window():
    rs = stub_adapter("rs")
    rs.functions = dict(overworld=1, battle=2, wait=3, action=(4,), move=(5,), yes_no=(0x80B5505,), menu=(6,))
    question = tables.encode("Save?") + b"\xff"
    rs.rom = bytearray(0x200)
    rs.rom[0x100 : 0x100 + len(question)] = question
    rs.rom[0x180:0x186] = tables.encode("Other") + b"\xff"
    layout = LAYOUTS["rs"]
    field = bytearray(0x30)  # finished (state 0), still holding its text
    struct.pack_into("<I", field, 0x20, 0x08000100)
    menu = bytearray(0x30)
    struct.pack_into("<I", menu, 0x20, 0x08000180)
    task = bytearray(0x28)
    struct.pack_into("<I", task, 0, 0x80B5505)
    task[4] = 1
    emu = StubEmu(
        **{
            f"x{layout.FIELD_WINDOW:x}": bytes(field),
            f"x{layout.MENU_WINDOW:x}": bytes(menu),
            f"x{layout.TASKS:x}": bytes(task),
        }
    )
    screen = rs._screen(emu, False, True)
    assert screen["yes_no"] and screen["text"] == ["Save?"]
