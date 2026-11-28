"""Gen 1 adapter. The first group needs no ROM; the rest boot a new game on the user's own ROM (skipped without one)."""

import random

import pytest

from pokeagent.emulator.client import Emulator
from pokeagent.emulator.cores import core_path
from pokeagent.games.pokemon_gen1 import CHARMAP, CORE_OPTIONS, DEX_OWNED, PARTY_COUNT, Gen1, Grid, bcd, decode
from pokeagent.play import Game

ENCODE = {v: k for k, v in CHARMAP.items() if len(v) == 1}


def text(s: str) -> bytes:
    return bytes(ENCODE[c] for c in s)


def test_decode_and_bcd():
    assert decode(text("RED") + b"\x50" + text("X")) == "RED"
    assert bcd(b"\x01\x23\x45") == 12345
    assert bcd(b"\x04\x48\x4a") == 0  # power-on noise is not BCD


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


def test_new_game_starts_in_the_bedroom(game):
    gen1, emu, bedroom = game
    emu.load_state(bedroom)
    state = gen1.state(emu)
    assert (state["map"], state["x"], state["y"]) == ("Reds House 2F", 3, 6)
    assert {state["player_name"], state["rival_name"]} == {"RED", "BLUE"}  # Blue swaps the preset names
    assert (state["party"], state["badges"], state["money"]) == ([], 0, 3000)
    assert state["screen"]["mode"] == "overworld" and state["in_game"]
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
    _, px, py = gen1._position(emu)
    need = len(grid.path((px, py), door))
    assert gen1.walk_to(emu, *door, max_steps=need)["outcome"] == "arrived"  # the last permitted step counts
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


def test_save_game_writes_a_standard_save_file(game):
    """Saving through the START menu fills the cartridge RAM other emulators load as a .sav: the player's name and a
    checksum the game itself checks before it offers CONTINUE."""
    gen1, emu, bedroom = game
    emu.load_state(bedroom)
    assert not gen1.save_complete(emu.save_ram())  # never saved
    assert gen1.save_game(emu)
    sram = emu.save_ram()
    assert len(sram) == 0x8000 and gen1.save_complete(sram)
    assert decode(sram[0x2598:0x25A3]) == gen1.state(emu)["player_name"]
    assert gen1.save_game(emu)  # a second save also confirms erasing the older file
    assert not gen1.save_complete(sram[:0x3000] + bytes([sram[0x3000] ^ 1]) + sram[0x3001:])
    gen1.press(emu, "start", wait=30)
    assert gen1.choose(emu, "SAVE")
    assert not gen1.save_game(emu)  # at a yes/no prompt, which it never answers
    emu.load_state(bedroom)
    gen1.press(emu, "start", wait=30)
    assert gen1.save_game(emu)  # backs out of an open menu first


def test_power_on_is_not_a_game_in_progress(rom_path):
    gen1 = Gen1(rom_path.read_bytes())
    with Emulator(core_path("sameboy"), rom_path, options=CORE_OPTIONS) as emu:
        state = gen1.state(emu)
        assert not state["in_game"] and not gen1.observations(state)["in_game"]
        assert gen1.state_text(state).startswith("No game is in progress yet")
        with pytest.raises(ValueError, match="no game is in progress"):
            Game(emu, gen1).act("walk", direction="up", steps=1)


def test_state_tolerates_any_ram(rom_path):
    """SameBoy randomises WRAM at power-on, so state() must never fail on arbitrary bytes."""
    gen1 = Gen1(rom_path.read_bytes())

    class Noise:
        def __init__(self, seed):
            self.rng, self.mem = random.Random(seed), {}

        def read(self, addr, n):
            return bytes(self.mem.setdefault(a, self.rng.randrange(256)) for a in range(addr, addr + n))

    for seed in range(200):
        state = gen1.state(Noise(seed))
        gen1.state_text(state), gen1.observations(state), gen1.fingerprint(state)


def test_nothing_is_owned_before_the_party_has_a_member(rom_path):
    gen1 = Gen1(rom_path.read_bytes())

    class Ram:
        def __init__(self, party_count):
            self.mem = {PARTY_COUNT: party_count, DEX_OWNED: 0b01001011}  # the starter balls' display flags

        def read(self, addr, n):
            return bytes(self.mem.get(a, 0) for a in range(addr, addr + n))

    assert gen1.state(Ram(0))["dex_owned"] == 0
    assert gen1.state(Ram(1))["dex_owned"] == 4


def test_press_waits_for_a_menu_to_react(game):
    # Live run 1a: A on a prompt is accepted, but the prompt box stays on screen for 16 frames, so a result taken
    # sooner shows a stale menu and the agent presses again.
    gen1, emu, bedroom = game
    emu.load_state(bedroom)
    gen1.press(emu, "start", wait=30)
    assert gen1.choose(emu, "SAVE")
    assert gen1.advance_text(emu)["outcome"] == "menu"
    gen1.press(emu, "a")  # YES
    assert not gen1.state(emu)["screen"]["yes_no"]
