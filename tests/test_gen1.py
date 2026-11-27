"""Gen 1 adapter. The first group needs no ROM; the rest boot a new game on the user's own ROM (skipped without one)."""

import pytest

from pokeagent.emulator.client import Emulator
from pokeagent.emulator.cores import core_path
from pokeagent.games.pokemon_gen1 import CHARMAP, CORE_OPTIONS, Gen1, Grid, bcd, decode

ENCODE = {v: k for k, v in CHARMAP.items() if len(v) == 1}


def text(s: str) -> bytes:
    return bytes(ENCODE[c] for c in s)


def test_decode_and_bcd():
    assert decode(text("RED") + b"\x50" + text("X")) == "RED"
    assert bcd(b"\x01\x23\x45") == 12345


def test_unsupported_rom_is_refused():
    with pytest.raises(ValueError, match="unsupported ROM"):
        Gen1(b"not a rom")


def test_grid_paths_around_walls_sprites_and_warps():
    floor, wall = 1, 2
    rows = [[floor, floor, wall, floor, floor], [floor, floor, wall, floor, floor], [floor] * 5]
    grid = Grid(5, 3, rows, frozenset({floor}), frozenset(), {})
    route = grid.path((0, 0), (4, 0))
    assert len(route) == 8 and route.count("down") == route.count("up") == 2
    grid.warps = {(2, 0)}  # a warp is walkable even on a wall tile
    assert grid.path((0, 0), (4, 0)) == ["right"] * 4
    grid.warps, grid.sprites = set(), {(2, 2)}
    assert grid.path((0, 0), (4, 0)) is None
    assert "@" in grid.render((0, 0)) and "S" in grid.render((0, 0))


def test_grid_ledges_are_one_way_and_tile_pairs_block():
    floor, ledge, water = 1, 3, 4
    grid = Grid(1, 3, [[floor], [ledge], [floor]], frozenset({floor}), frozenset(), {("down", floor, ledge): "down"})
    assert grid.path((0, 0), (0, 2)) == ["down"]
    assert grid.path((0, 2), (0, 0)) is None
    grid = Grid(2, 1, [[floor, water]], frozenset({floor, water}), frozenset({(floor, water)}), {})
    assert grid.path((0, 0), (1, 0)) is None


def test_screen_mode():
    blank = bytearray(text(" ") * 360)
    assert Gen1.screen_mode(bytes(blank), False)["mode"] == "overworld"
    dialog = bytearray(blank)
    dialog[12 * 20], dialog[17 * 20 + 19], dialog[16 * 20 + 18] = 0x79, 0x7E, 0xEE
    assert Gen1.screen_mode(bytes(dialog), False) | {"cursor_at": None} == {
        "mode": "dialog",
        "cursor": False,
        "cursor_at": None,
        "yes_no": False,
        "waiting": True,
    }
    prompt = bytearray(dialog)
    prompt[14 * 20 + 1 : 14 * 20 + 5] = b"\xed" + text("YES")
    prompt[15 * 20 + 2 : 15 * 20 + 4] = text("NO")
    mode = Gen1.screen_mode(bytes(prompt), False)
    assert (mode["mode"], mode["cursor_at"], mode["yes_no"]) == ("menu", [1, 14], True)
    assert Gen1.screen_mode(bytes(prompt), True)["mode"] == "battle"


# ---- on the user's ROM ----


@pytest.fixture(scope="module")
def game(rom_path):
    """(adapter, emulator, state of a new game standing in the bedroom). Boots once; tests load the state."""
    gen1 = Gen1(rom_path.read_bytes())
    with Emulator(core_path("sameboy"), rom_path, options=CORE_OPTIONS) as emu:
        emu.run([], 300)
        for _ in range(60):  # title screen until the CONTINUE/NEW GAME menu
            if gen1.screen_mode(gen1.screen_tiles(emu), False)["cursor"]:
                break
            gen1.press(emu, "start", wait=40)
        gen1.press(emu, "a", wait=30)
        names = 0
        for _ in range(40):  # Oak's speech; take the first preset name (below NEW NAME) for player and rival
            outcome = gen1.advance_text(emu)["outcome"]
            state = gen1.state(emu)
            if outcome == "menu":
                x, y = state["screen"]["cursor_at"]
                assert gen1.choose(emu, gen1.screen_text(gen1.screen_tiles(emu))[y + 2][x + 1 :].split()[0])
                names += 1
            elif names == 2 and state["map_size"] != [0, 0]:
                break
        yield gen1, emu, emu.save_state()


def test_new_game_starts_in_the_bedroom(game):
    gen1, emu, bedroom = game
    emu.load_state(bedroom)
    state = gen1.state(emu)
    assert (state["map"], state["x"], state["y"]) == ("Reds House 2F", 3, 6)
    assert {state["player_name"], state["rival_name"]} == {"RED", "BLUE"}  # Blue swaps the preset names
    assert (state["party"], state["badges"], state["money"]) == ([], 0, 3000)
    assert state["screen"]["mode"] == "overworld"
    assert (gen1.species_name(0x99), gen1.item_name(0x04), gen1.move_name(1)) == ("BULBASAUR", "POKé BALL", "POUND")


def test_walks_downstairs_and_out_of_the_house(game):
    gen1, emu, bedroom = game
    emu.load_state(bedroom)
    grid = gen1.grid(emu)
    assert (7, 1) in grid.warps and not grid.walkable(0, 0)
    assert gen1.walk_to(emu, 7, 1)["outcome"] == "map_changed"
    assert gen1.state(emu)["map"] == "Reds House 1F"
    grid = gen1.grid(emu)
    assert (5, 4) in grid.sprites  # Mom
    door = max(grid.warps, key=lambda w: w[1])
    assert gen1.walk_to(emu, *door)["outcome"] == "arrived"
    assert gen1.walk(emu, "down", 1)["outcome"] == "map_changed"
    state = gen1.state(emu)
    assert state["map"] == "Pallet Town" and state["screen"]["mode"] == "overworld"
    # Heading north towards the tall grass triggers Oak's event a moment after arriving.
    assert gen1.walk_to(emu, 10, 1)["outcome"] == "arrived"
    emu.run([], 60)
    assert gen1.state(emu)["screen"]["mode"] == "dialog"
    assert gen1.advance_text(emu)["presses"] > 0


def test_advance_text_never_answers_a_prompt(game):
    gen1, emu, bedroom = game
    emu.load_state(bedroom)
    gen1.press(emu, "start", wait=30)
    assert gen1.choose(emu, "SAVE")
    assert gen1.advance_text(emu) == {"outcome": "menu", "presses": 0}
    assert gen1.state(emu)["screen"]["yes_no"]


def test_state_restores_the_in_game_save(game):
    """Cartridge RAM is part of the core's state, so an in-game save cannot leak from one timeline to another."""
    gen1, emu, bedroom = game
    emu.load_state(bedroom)
    before = emu.ram_hash()  # covers work RAM and the whole cartridge RAM
    gen1.press(emu, "start", wait=30)
    assert gen1.choose(emu, "SAVE")
    gen1.advance_text(emu)
    assert gen1.choose(emu, "YES")
    assert gen1.advance_text(emu)["outcome"] == "closed"
    emu.load_state(bedroom)
    assert emu.ram_hash() == before
