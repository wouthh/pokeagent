"""Gen 1 versions: Red, Blue, Yellow and hacks of their engine are told apart by scanning the ROM itself.
The ROM tests use dumps in the main checkout's ignored roms/ folder and skip without them."""

import pytest

from pokeagent.games import identify
from pokeagent.games.gen1_tables import find_tables
from pokeagent.games.pokemon_gen1 import RAM_LAYOUTS, Gen1, layout_problems
from pokeagent.play import draw_variety

RED_BLUE = {
    "species": (0x07, 0x421E),
    "items": (0x01, 0x472B),
    "moves": (0x2C, 0x4000),
    "types": (0x09, 0x7DAE),
    "tile_pairs": (0x00, 0x0C7E),
    "ledges": (0x06, 0x66CF),
}
YELLOW = {
    "species": (0x3A, 0x4000),
    "items": (0x01, 0x45B7),
    "moves": (0x2F, 0x4000),
    "types": (0x09, 0x7D63),
    "tile_pairs": (0x00, 0x0ADA),
    "ledges": (0x06, 0x6851),
}


def test_the_yellow_layout_is_one_lower_from_the_walk_counter_on():
    red, yellow = RAM_LAYOUTS["red-blue"], RAM_LAYOUTS["yellow"]
    assert yellow["WALK_COUNTER"] == red["WALK_COUNTER"] - 1 == 0xCFC4
    assert all(yellow[name] == red[name] - 1 for name in red)
    assert yellow["PARTY_COUNT"] == 0xD162 and yellow["CUR_MAP"] == 0xD35D


def test_noise_and_other_bytes_are_not_a_gen_1_game():
    assert find_tables(b"not a rom") is None and not Gen1.recognises(bytes(1 << 20))
    with pytest.raises(ValueError, match="unsupported ROM"):
        identify(bytes(1 << 20))


@pytest.mark.parametrize(
    ("name", "tables"),
    [
        ("Pokemon Red.gb", RED_BLUE),
        ("Pokemon Blue.gb", RED_BLUE),
        ("Pokemon Yellow.gbc", YELLOW),
        ("01 Pokemon Blue Kaizo Version.gb", RED_BLUE),  # a Blue hack: gameplay data changed, the tables did not move
    ],
)
def test_the_scanner_finds_the_known_tables(find_rom, name, tables):
    rom = find_rom(name).read_bytes()
    assert find_tables(rom) == tables and Gen1.recognises(rom)


@pytest.mark.parametrize("name", ["Pokemon Gold.gbc", "Pokemon Crystal.gbc"])
def test_gen_2_is_not_recognised_as_gen_1(find_rom, name):
    assert not Gen1.recognises(find_rom(name).read_bytes())


def test_an_unknown_dump_of_the_engine_is_a_named_hack(find_rom):
    adapter = identify(find_rom("01 Pokemon Blue Kaizo Version.gb").read_bytes())
    assert adapter.title == "Gen 1 ROM hack (header title POKEMON BLUE)"
    assert (adapter.layout, adapter.yellow) == ("red-blue", False)
    assert adapter.species_name(0x99) == "BULBASAUR" and adapter.CORE_OPTIONS == {"sameboy_model": "Game Boy"}


def test_yellow_has_its_own_play_data(find_rom):
    adapter = identify(find_rom("Pokemon Yellow.gbc").read_bytes())
    assert adapter.title == "Pokémon Yellow (English)" and adapter.layout == "yellow"
    assert adapter.CORE_OPTIONS == {"sameboy_model": "Game Boy Color"}
    assert adapter.STARTERS == {0x54: (0x17,)} and adapter.species_name(0x54) == "PIKACHU"
    assert adapter.types[0x17] == "ELECTRIC" and adapter.map_names[0x28] == "Oaks Lab"
    assert len(adapter.map_names) == 249 and not any("Safari" in q for q in adapter.SIDE_QUESTS)
    assert (adapter.species_name(1), adapter.item_name(1), adapter.move_name(1)) == ("RHYDON", "MASTER BALL", "POUND")


def test_a_yellow_run_starts_with_pikachu_and_never_draws_electric(find_rom):
    adapter = identify(find_rom("Pokemon Yellow.gbc").read_bytes())
    for _ in range(20):
        variety = draw_variety(adapter, [])
        assert variety["starter"] == "PIKACHU" and "ELECTRIC" not in variety["team_types"]


def test_yellow_boots_to_the_bedroom_and_walks_out(yellow_game):
    gen1, emu, bedroom = yellow_game
    emu.load_state(bedroom)
    state = gen1.state(emu)
    assert (state["map"], state["x"], state["y"]) == ("Reds House 2F", 3, 6)
    assert (state["player_name"], state["rival_name"]) == ("YELLOW", "BLUE")
    assert (state["party"], state["badges"], state["money"]) == ([], 0, 3000) and state["in_game"]
    assert gen1.layout_checked and not state["layout_unverified"]
    assert gen1.walk_to(emu, 7, 1)["outcome"] == "map_changed"
    assert gen1.state(emu)["map"] == "Reds House 1F"
    assert not layout_problems(emu.read, RAM_LAYOUTS["yellow"]) and layout_problems(emu.read, RAM_LAYOUTS["red-blue"])


def test_a_yellow_hack_with_another_title_is_still_yellow(find_rom):
    rom = bytearray(find_rom("Pokemon Yellow.gbc").read_bytes())
    rom[0x134:0x143] = b"POKEMON HACK".ljust(15, b"\0")
    adapter = Gen1(bytes(rom))
    assert adapter.yellow and adapter.layout == "yellow" and adapter.STARTERS == {0x54: (0x17,)}


def test_the_first_state_read_corrects_a_wrong_layout_guess(yellow_game):
    gen1, emu, bedroom = yellow_game
    emu.load_state(bedroom)
    hack = Gen1(gen1.rom)
    hack._set_base(False)  # as for a Yellow hack the first guess missed
    assert hack.state(emu)["player_name"] == "YELLOW" and hack.layout == "yellow"
    assert hack.yellow and hack.STARTERS == {0x54: (0x17,)} and len(hack.map_names) == 249


def test_the_colour_flag_picks_hardware_not_the_base_game(find_rom):
    rom = bytearray(find_rom("01 Pokemon Blue Kaizo Version.gb").read_bytes())
    rom[0x143] = 0x80  # a Red hack with Game Boy Color support
    adapter = Gen1(bytes(rom))
    assert adapter.CORE_OPTIONS == {"sameboy_model": "Game Boy Color"}
    assert not adapter.yellow and adapter.layout == "red-blue" and 0x99 in adapter.STARTERS


def test_an_unknown_layout_is_reported_not_refused(yellow_game):
    gen1, emu, bedroom = yellow_game
    emu.load_state(bedroom)

    class Odd:
        """The same game with the tileset header zeroed, so neither known layout fits."""

        def read(self, addr, n):
            data = bytearray(emu.read(addr, n))
            for a in range(max(addr, 0xD52A), min(addr + n, 0xD531)):
                data[a - addr] = 0
            return bytes(data)

    hack = Gen1(gen1.rom)
    state = hack.state(Odd())
    assert state["in_game"] and state["layout_unverified"] and not hack.layout_checked
    assert "memory layout is unknown" in hack.state_text(state)
    assert not gen1.state(emu)["layout_unverified"]


def test_pikachu_never_blocks_the_walk_grid_in_yellow(yellow_game):
    gen1, emu, bedroom = yellow_game
    emu.load_state(bedroom)

    class Follower:
        """The emulator with Pikachu's sprite slot (15) made visible one step from the player, at (4, 3)."""

        def read(self, addr, n):
            data = bytearray(emu.read(addr, n))
            for base, patch in ((0xC1F0, {0: 0x49, 2: 0}), (0xC2F4 - 4, {4: 3 + 4, 5: 4 + 4})):
                for off, value in patch.items():
                    if addr <= base + off < addr + n:
                        data[base + off - addr] = value
            return bytes(data)

    # the follower is visible on the screen grid in Red/Blue terms, and ignored in Yellow
    assert (4, 3) not in gen1.grid(Follower()).sprites
    gen1.yellow = False
    try:
        assert (4, 3) in gen1.grid(Follower()).sprites
    finally:
        gen1.yellow = True
