"""Gen 2 adapter: byte-level tests on synthetic memory, then ROM tests that skip when a ROM is not in roms/.

ROMs are read from the main checkout's ignored roms/ folder (the `find_rom` fixture).
"""

import pytest

from pokeagent.emulator.client import Emulator
from pokeagent.emulator.cores import core_path
from pokeagent.games import identify
from pokeagent.games.gen2_tables import find_tables
from pokeagent.games.pokemon_gen1 import Gen1
from pokeagent.games.pokemon_gen2 import (
    CORE_OPTIONS,
    HALL_OF_FAME,
    RAM_LAYOUTS,
    SAVE_LAYOUTS,
    Gen2,
    Gen2Grid,
    layout_problems,
)

GOLD = RAM_LAYOUTS["gold-silver"]
BOX = [0x79] + [0x7A] * 18 + [0x7B]  # ┌────┐
ROM_NAMES = {
    "gold": "Pokemon Gold.gbc",
    "silver": "Pokemon Silver.gbc",
    "crystal": "Pokemon Crystal.gbc",
    "clear": "02 Pokemon Crystal Clear 2.5.1.gbc",
}


def encode(text: str) -> list[int]:
    charmap = {v: k for k, v in Gen2.CHARMAP.items() if len(v) == 1}
    return [charmap[c] for c in text]


def tiles_with(*rows: tuple[int, list[int]]) -> bytes:
    tiles = bytearray([0x01] * 360)  # floor tiles
    for y, row in rows:
        tiles[y * 20 : y * 20 + len(row)] = bytes(row)
    return bytes(tiles)


def dialog(*lines: str) -> bytes:
    """A dialog box in rows 12-17: border, blank, line, blank, line, border."""
    side = [0x7C] + [0x7F] * 18 + [0x7C]
    rows = [(12, [0x79] + [0x7A] * 18 + [0x7B])] + [(r, side[:]) for r in range(13, 17)]
    rows.append((17, [0x7D] + [0x7A] * 18 + [0x7E]))
    tiles = bytearray(tiles_with(*rows))
    for y, line in zip((14, 16), lines, strict=False):
        tiles[y * 20 + 1 : y * 20 + 1 + len(line)] = bytes(encode(line))
    return bytes(tiles)


def test_screen_text_only_inside_boxes():
    tiles = bytearray(dialog("Hello there"))
    tiles[2 * 20 : 2 * 20 + 4] = bytes(encode("Mapx"))  # a map tile row that decodes as letters
    text = Gen2.screen_text(bytes(tiles))
    assert text[14] == " Hello there" and not text[2] and not any(text[:12])


def test_screen_text_skips_pictures():
    side = lambda r: [0x7C, *encode("ABCDEFG"), 0x7C]  # noqa: E731  (a picture has no blanks)
    rows = (
        [(1, [0x79] + [0x7A] * 7 + [0x7B])] + [(r, side(r)) for r in range(2, 8)] + [(8, [0x7D] + [0x7A] * 7 + [0x7E])]
    )
    assert not any(Gen2.screen_text(tiles_with(*rows)))


def test_screen_mode_arrow_is_on_row_17():
    tiles = bytearray(dialog("Hello"))
    assert Gen2.screen_mode(bytes(tiles), False)["mode"] == "dialog"
    assert not Gen2.screen_mode(bytes(tiles), False)["waiting"]
    tiles[17 * 20 + 18] = 0xEE
    assert Gen2.screen_mode(bytes(tiles), False)["waiting"]
    tiles[16 * 20 + 18] = 0xEE  # Gen 1's row is not the arrow here
    assert Gen2.screen_mode(bytes(tiles), True)["mode"] == "battle"


def test_yes_no_box_and_cursor():
    box = [
        (7, [0x79] + [0x7A] * 4 + [0x7B]),
        (8, [0x7C, 0xED, *encode("YES"), 0x7C]),
        (9, [0x7C] + [0x7F] * 4 + [0x7C]),
        (10, [0x7C, 0x7F, *encode("NO"), 0x7F, 0x7C]),
        (11, [0x7D] + [0x7A] * 4 + [0x7E]),
    ]
    tiles = bytearray(tiles_with(*[(y, [0x7F] * 14 + row) for y, row in box]))
    mode = Gen2.screen_mode(bytes(tiles), False)
    assert mode["yes_no"] and mode["cursor"] and mode["cursor_at"] == [15, 8]


def test_charmap_shifts_from_gen1():
    assert Gen2.decode(bytes([0x93, 0xA7, 0xA4, 0xD1, 0xD1, 0xEA, 0x50, 0x80])) == "The'l'lé"
    assert Gen2.decode(bytes([0xF6, 0xFF, 0xF1, 0xE8])) == "09\u00d7."
    assert Gen1.decode(bytes([0xBA])) == "é" and Gen2.decode(bytes([0xBA])) == "·"  # 0xBA is kana here


def grid(rows: list[str], hops: dict | None = None) -> Gen2Grid:
    """`.` floor, `#` wall, `v`/`>` hop down/right (0xA3/0xA0), `b` a side wall (0xB2) blocking entry going down."""
    codes = {".": 0x00, "#": 0x07, "v": 0xA3, ">": 0xA0, "b": 0xB2, "<": 0xA1}
    tiles = [[codes[c] for c in row] for row in rows]
    passable = frozenset({0x00})
    return Gen2Grid(len(rows[0]), len(rows), tiles, passable, frozenset(), {}, set(), set(), 0x18)


def test_render_marks_water_that_needs_surf():
    g = grid([".", "#", "."])
    g.water = frozenset({0x07})  # stand-in for a water byte
    assert g.render((0, 0)).split("\n")[2][4] == "~" and g.render((0, 0), radius=1).split("\n")[2][4] == "~"


def test_hop_down_is_one_way():
    g = grid([".", "v", "#", ".", "."])
    assert g.path((0, 0), (0, 3)) == ["down", "down"]  # onto the ledge, then the jump over the wall
    assert g.path((0, 3), (0, 0)) is None
    assert g.path((0, 3), (0, 1)) is None
    assert ("down", (0, 3)) in list(g.moves(0, 1))


def test_hop_right_and_left_directions():
    g = grid([".>#.", "..#."])
    assert g.path((0, 0), (3, 0)) == ["right", "right"]
    assert g.path((3, 0), (0, 0)) is None
    g = grid([".#<."])
    assert g.path((3, 0), (0, 0)) == ["left", "left"]


def test_hop_does_not_replace_a_plain_step():
    g = grid(["v", ".", "."])  # open floor below the ledge: a plain step
    assert [m for m in g.moves(0, 0) if m[0] == "down"] == [("down", (0, 1))]


def test_side_wall_blocks_one_direction():
    g = grid(["..", "b."])
    assert "down" not in [d for d, _ in g.moves(0, 0)]  # an UP wall: cannot enter moving down
    assert "right" in [d for d, _ in g.moves(0, 1)] and "up" in [d for d, _ in g.moves(0, 1)]
    assert g.path((0, 0), (0, 1)) == ["right", "down", "left"]


class Memory:
    """A fake emulator over a flat byte array, for the Gold layout."""

    def __init__(self):
        self.mem = bytearray(0x10000)
        self.mem[0xFF70] = 1

    def read(self, addr: int, n: int) -> bytes:
        return bytes(self.mem[addr : addr + n])

    def put(self, addr: int, data: bytes) -> None:
        self.mem[addr : addr + len(data)] = data


def fake_adapter(layout: str = "gold-silver") -> Gen2:
    g = object.__new__(Gen2)
    g.known, g.crystal, g.layout, g.layout_checked, g.tm_items, g._areas = (
        True,
        layout == "crystal",
        layout,
        True,
        list(range(191, 241)) + list(range(243, 250)),  # TM01-TM50, then HM01-HM07 after unused ids
        {},
    )
    g.ram = type("Ram", (), RAM_LAYOUTS[layout])
    g.species = [f"MON{i}" for i in range(1, 252)]
    g.items = [f"ITEM{i}" for i in range(1, 250)]
    g.moves = [f"MOVE{i}" for i in range(1, 252)]
    g.types = [f"TYPE{i}" for i in range(0x1C)]
    rom = bytearray(0x4000)
    rom[0x100 + 7 : 0x100 + 9] = bytes([0x14, 0x03])  # species 1's types
    g.rom, g.base_data, g.map_names = bytes(rom), (0, 0x100), {0x0107: "Players House 2F"}
    return g


def started_game(layout: str = "gold-silver") -> Memory:
    m, r = Memory(), RAM_LAYOUTS[layout]
    m.put(r["PLAYER_NAME"], bytes([*encode("RED"), 0x50]))
    m.put(r["MAP_GROUP"], bytes([1, 7, 5, 4]))  # group 1, map 7, y 5, x 4
    m.put(r["MAP_HEIGHT"], bytes([3, 4]))
    m.put(r["TILESET"], bytes([0x37, 0xD3, 0x73, 0x37, 0x93, 0x78, 0x37, 0x93, 0x7C]))
    m.put(r["FACING"], bytes([0x08]))
    m.put(r["MONEY"], (123456).to_bytes(3))
    m.put(r["BADGES"], bytes([0b101, 0b11]))
    mon = bytearray(0x30)
    mon[0:2] = bytes([1, 173])  # species 1 holding item 173
    mon[2:6] = bytes([5, 6, 0, 0])
    mon[8:11] = (1234).to_bytes(3)
    mon[11:21] = b"".join(v.to_bytes(2) for v in (1, 2, 3, 4, 5))  # stat exp HP Atk Def Spd Spc
    mon[0x15:0x17] = bytes([0xFA, 0xDC])  # DVs
    mon[0x17:0x1B] = bytes([0x23, 0x45, 0, 0])  # PP: 35 and 5 with one PP Up
    mon[0x1F], mon[0x20] = 17, 0x08  # level, poisoned
    mon[0x22:0x24], mon[0x24:0x26] = (30).to_bytes(2), (40).to_bytes(2)
    mon[0x26:0x30] = b"".join(v.to_bytes(2) for v in (10, 11, 12, 13, 14))
    m.put(r["PARTY_COUNT"], bytes([1, 1, 0xFF]))
    m.put(r["PARTY_MONS"], bytes(mon))
    m.put(r["PARTY_NICKS"], bytes([*encode("SPARKY"), 0x50]))
    m.put(r["NUM_ITEMS"], bytes([1, 4, 9, 0xFF]))
    m.put(r["NUM_KEY_ITEMS"], bytes([2, 7, 8, 0xFF]))
    m.put(r["NUM_BALLS"], bytes([1, 2, 6, 0xFF]))
    m.put(r["TMHM"] + 2, bytes([3]))  # three of the third TM
    m.put(r["TMHM"] + 56, bytes([1]))  # HM07, the last slot
    m.put(r["DEX_OWNED"], bytes([0b1111, 0, 0] + [0] * 28 + [0xFF]))  # the last byte's high bits are not Pokémon
    m.put(r["DEX_SEEN"], bytes([0xFF]))
    m.put(r["TIME_OF_DAY"], bytes([2]))
    return m


def test_state_decodes_gen2_structs():
    g, m = fake_adapter(), started_game()
    state = g.state(m)
    assert state["in_game"] and state["map_id"] == 0x0107 and state["map"] == "Players House 2F"
    assert (state["x"], state["y"], state["facing"], state["map_size"]) == (4, 5, "left", [8, 6])
    assert state["money"] == 123456 and state["player_name"] == "RED"
    assert (state["badges"], state["badge_bits"], state["badge_max"]) == (4, 0b11_00000101, 16)
    assert "Johto 2, Kanto 2" in state["badge_note"] and state["time_of_day"] == "night"
    assert state["dex_owned"] == 4 + 3 and state["dex_seen"] == 8  # 3 valid bits in the last byte
    mon = state["party"][0]
    assert (mon["species"], mon["name"], mon["level"], mon["hp"], mon["max_hp"], mon["status"]) == (
        "MON1",
        "SPARKY",
        17,
        30,
        40,
        "poisoned",
    )
    assert mon["types"] == ["TYPE20", "TYPE3"] and mon["item"] == "ITEM173" and mon["exp"] == 1234  # fmt: skip
    assert mon["moves"] == ["MOVE5", "MOVE6"] and mon["pp"] == [(35, 0), (5, 1)]
    assert mon["stats"] == [10, 11, 12, 13, 14] and mon["stat_exp"] == [2, 3, 4, 5, 1]  # Atk Def Spd Spc HP
    assert mon["dvs"] == [15, 10, 13, 12, 8 | 0 | 2 | 0]  # HP DV from the low bits (1, 0, 1, 0)
    assert state["bag"] == [
        {"item": "ITEM4", "qty": 9},
        {"item": "ITEM7", "qty": 1},
        {"item": "ITEM8", "qty": 1},
        {"item": "ITEM2", "qty": 6},
        {"item": "ITEM193", "qty": 3},  # TM03 when TM01 is item 191
        {"item": "ITEM249", "qty": 1},  # HM07: unused ids sit between TM50 and HM01
    ]
    assert state["battle"] is None


def test_state_battle_kind_and_money_is_not_bcd():
    g, m = fake_adapter("crystal"), started_game("crystal")
    r = RAM_LAYOUTS["crystal"]
    m.put(r["IS_IN_BATTLE"], bytes([2]))
    m.put(r["ENEMY"], bytes([1]) + bytes(12) + bytes([9, 0, 0]) + (20).to_bytes(2) + (25).to_bytes(2))
    state = g.state(m)
    assert state["battle"]["kind"] == "trainer" and state["battle"]["enemy_level"] == 9
    assert (state["battle"]["enemy_hp"], state["battle"]["enemy_max_hp"]) == (20, 25)
    assert state["screen"]["mode"] == "battle"


def test_game_state_is_not_read_from_the_wrong_wram_bank():
    g, m = fake_adapter(), started_game()
    m.mem[0xFF70] = 5  # the title screen maps bank 5 at D000
    state = g.state(m)
    assert not state["in_game"] and state["party"] == [] and state["bag"] == [] and state["money"] == 0
    m.mem[0xFF70] = 0xF8  # bank 0 selects bank 1; the upper bits read as 1
    assert g.state(m)["in_game"]


def test_power_on_noise_is_not_a_game():
    g, m = fake_adapter(), Memory()
    for i in range(0xC000, 0xE000):
        m.mem[i] = (i * 7) & 0xFF
    state = g.state(m)
    assert not state["in_game"] and state["battle"] is None


def test_layout_probe_tells_the_layouts_apart():
    m = started_game("crystal")
    assert layout_problems(m.read, RAM_LAYOUTS["crystal"]) == []
    assert layout_problems(m.read, GOLD)  # Gold's addresses read noise in Crystal's RAM
    m = started_game()
    m.put(GOLD["PARTY_COUNT"], bytes([7]))
    assert "party list" in layout_problems(m.read, GOLD)


def make_sram(layout: str, name: str = "RED") -> bytes:
    s, spec = bytearray(0x8000), SAVE_LAYOUTS[layout]
    s[0x2008], s[spec["check_2"]] = 99, 127
    s[0x200B : 0x200B + len(name) + 1] = bytes([*encode(name), 0x50])
    s[spec["checksum"] : spec["checksum"] + 2] = (sum(s[0x2009 : spec["end"]]) & 0xFFFF).to_bytes(2, "little")
    return bytes(s)


@pytest.mark.parametrize("layout", ["gold-silver", "crystal"])
def test_save_complete(layout):
    g = fake_adapter(layout)
    sram = make_sram(layout)
    assert g.save_complete(sram)
    spec = SAVE_LAYOUTS[layout]
    assert not g.save_complete(sram[:-1])
    assert not g.save_complete(sram[:0x2100] + b"\x01" + sram[0x2101:])  # data changed, checksum stale
    assert not g.save_complete(sram[:0x2008] + b"\x00" + sram[0x2009:])  # check value 1
    assert not g.save_complete(sram[: spec["check_2"]] + b"\x00" + sram[spec["check_2"] + 1 :])
    assert not g.save_complete(make_sram(layout, ""))  # no player name: a blank cartridge


def test_save_layouts_differ_per_game():
    assert SAVE_LAYOUTS["gold-silver"]["checksum"] != SAVE_LAYOUTS["crystal"]["checksum"]
    assert not fake_adapter("crystal").save_complete(make_sram("gold-silver"))


def test_find_tables_rejects_other_data():
    assert find_tables(bytes(0x20000)) is None
    assert not Gen2.recognises(bytes(0x20000))


def test_hall_of_fame_is_the_default_check():
    assert HALL_OF_FAME == 0x1008
    assert Gen2.DEFAULT_CHECK == {"obs": "map_id", "op": "==", "value": HALL_OF_FAME}
    assert CORE_OPTIONS == {"sameboy_model": "Game Boy Color", "sameboy_rtc": "accurate"}


# ---- ROM tests ----


@pytest.mark.parametrize(
    ("name", "title", "layout"),
    [
        ("gold", "Pokémon Gold (English)", "gold-silver"),
        ("silver", "Pokémon Silver (English)", "gold-silver"),
        ("crystal", "Pokémon Crystal (English 1.1)", "crystal"),
    ],
)
def test_identify_and_tables(find_rom, name, title, layout):
    path = find_rom(ROM_NAMES[name])
    gen2 = identify(path.read_bytes())
    assert isinstance(gen2, Gen2) and gen2.title == title and gen2.layout == layout
    assert gen2.species[0] == "BULBASAUR" and gen2.species[250] == "CELEBI" and gen2.species[154] == "CYNDAQUIL"
    assert gen2.moves[0] == "POUND" and gen2.moves[250] == "BEAT UP" and gen2.items[0] == "MASTER BALL"
    assert gen2.items[190] == "TM01" and gen2.items[248] == "HM07"
    assert gen2.types[0] == "NORMAL" and gen2.types[0x14] == "FIRE" and gen2.types[0x1B] == "DARK"
    assert gen2.base_types(155) == ["FIRE"] and gen2.base_types(1) == ["GRASS", "POISON"]
    assert gen2.permissions[0] == 0 and gen2.permissions[7] == 0x0F and gen2.permissions[0x12] == 0x1F
    assert gen2.map_names[0x1805] == "Elms Lab" and gen2.map_names[HALL_OF_FAME] == "Hall Of Fame"
    assert not Gen1.recognises(path.read_bytes())


def test_crystal_clear_is_found_by_scanning(find_rom):
    path = find_rom(ROM_NAMES["clear"])
    gen2 = identify(path.read_bytes())
    assert gen2.title.startswith("Gen 2 ROM") and gen2.layout == "crystal" and not gen2.known
    assert gen2.species[0] == "BULBASAUR" and gen2.moves[0] == "POUND" and gen2.base_types(155) == ["FIRE"]
    assert gen2.map_names == {}  # hacks get landmark names, not the dump's constants


def boot_new_game(gen2: Gen2, emu: Emulator) -> bytes:
    """Start a new game with blind A presses (defaults everywhere) to the bedroom and return the saved state."""
    emu.run([], 300)
    for i in range(1500):
        emu.run([], 20)
        emu.run(["a"], 4)
        if i % 4 == 0 and (state := gen2.state(emu))["in_game"] and state["screen"]["mode"] == "overworld":
            emu.run([], 90)
            if gen2.state(emu)["screen"]["mode"] == "overworld":
                return emu.save_state()
    raise AssertionError("no new game")


@pytest.fixture(scope="module", params=["gold", "crystal"])
def game(request, find_rom):
    path = find_rom(ROM_NAMES[request.param])
    gen2 = Gen2(path.read_bytes())
    with Emulator(core_path("sameboy"), path, options=gen2.CORE_OPTIONS) as emu:
        yield gen2, emu, boot_new_game(gen2, emu)


def test_power_on_is_not_a_game(find_rom):
    path = find_rom(ROM_NAMES["gold"])
    gen2 = Gen2(path.read_bytes())
    with Emulator(core_path("sameboy"), path, options=gen2.CORE_OPTIONS) as emu:
        emu.run([], 60)
        state = gen2.state(emu)
        assert not state["in_game"] and "No game is in progress yet" in gen2.state_text(state)


def test_new_game_state_shape(game):
    gen2, emu, start = game
    emu.load_state(start)
    state = gen2.state(emu)
    assert state["in_game"] and state["map"] == "Players House 2F" and state["map_id"] == 0x1807
    assert (state["x"], state["y"], state["facing"]) == (3, 3, "down") and state["map_size"] == [8, 6]
    assert state["money"] == 3000 and state["badges"] == 0 and state["party"] == [] and state["bag"] == []
    assert state["time_of_day"] in ("morning", "day", "night") and not state["layout_unverified"]
    text = gen2.state_text(state, detail=True)
    assert "badges 0/16 (Johto 0, Kanto 0)" in text and "Party: none" in text
    assert gen2.observations(state)["map_id"] == 0x1807 and gen2.fingerprint(state)["position"] == (0x1807, 3, 3)
    assert gen2.progress_score(state, 1, 1) > 0


def test_grid_of_the_bedroom(game):
    gen2, emu, start = game
    emu.load_state(start)
    grid = gen2.grid(emu)
    assert (grid.width, grid.height) == (8, 6)
    assert (7, 0) in grid.warps  # the stairs
    assert grid.path((3, 3), (7, 1)) is not None and grid.path((3, 3), (0, 0)) is None


def test_walk_to_stairs_and_advance_text_stops_at_a_prompt(game):
    gen2, emu, start = game
    emu.load_state(start)
    assert gen2.walk_to(emu, 7, 1)["outcome"] == "arrived"
    assert gen2.walk(emu, "up", 1)["outcome"] == "map_changed"
    assert gen2.state(emu)["map"] == "Players House 1F"
    outcomes = []
    for _ in range(6):  # Mom talks; she may ask for the day of the week (a yes/no prompt that is never answered)
        outcomes.append(gen2.advance_text(emu)["outcome"])
        if outcomes[-1] == "menu":
            assert gen2.state(emu)["screen"]["yes_no"] or gen2.state(emu)["screen"]["cursor"]
            break
    assert "menu" in outcomes or outcomes[-1] in ("closed", "no_text")


def test_save_game_and_save_complete(game):
    gen2, emu, start = game
    emu.load_state(start)
    assert not gen2.save_complete(emu.save_ram())
    assert gen2.save_game(emu)
    sram = emu.save_ram()
    assert gen2.save_complete(sram) and len(sram) == 0x8000
    assert gen2.decode(sram[0x200B : 0x200B + 8]) == gen2.state(emu)["player_name"]


def test_area_matches_the_full_attribute_pointer():
    g = fake_adapter()
    rom = bytearray(0x4000)
    rom[0x100:0x106] = bytes([1, 7, 0, 0x34, 0x99, 3])  # decoy: same low byte, other high byte
    rom[0x200:0x206] = bytes([1, 7, 0, 0x34, 0x12, 2])  # bank, tileset, environment, pointer, landmark 2
    rom[0x300 + 8 : 0x300 + 12] = bytes([0, 0, 0x00, 0x03])  # landmark 2: name pointer 0x0300... (see below)
    g.rom, g.landmarks = bytes(rom), (0, 0x300)
    g._offset = lambda addr: addr[1]
    g.decode = lambda raw: raw.decode("latin-1")
    rom[0x300 + 8 + 2 : 0x300 + 8 + 4] = (0x400).to_bytes(2, "little")
    rom[0x400:0x405] = b"HOME\x50"
    g.rom = bytes(rom)
    m = Memory()
    m.put(g.ram.MAP_ATTRIBUTES, bytes([1, 7, 0, 0x34, 0x12]))
    m.put(g.ram.TILESET, bytes([0x37]))  # the tileset's ROM bank differs from its id
    assert g.map_name(m, 0x0999) == "HOME (map 9:153)"


def test_find_tables_rejects_a_record_run_cut_off_by_the_rom_end():
    rom = bytearray(0x8000)
    rom[-64] = 1
    rom[-32] = 2
    assert find_tables(bytes(rom)) is None
