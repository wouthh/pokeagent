from pathlib import Path

import pytest
from PIL import Image, ImageDraw
from test_play import FakeGame, make_runner

from pokeagent import play as play_module
from pokeagent import stuck
from pokeagent.agents.claude_cli import SessionResult
from pokeagent.emulator.client import Emulator
from pokeagent.emulator.cores import core_path
from pokeagent.games import identify, pokemon_gen1, pokemon_gen2, pokemon_gen3
from pokeagent.games.generic import NOTE, Generic, frame_id, platform
from pokeagent.play import Game

ACID2 = Path(__file__).parent / "fixtures" / "dmg-acid2.gb"


def gba_header(title=b"HACKROM", code=b"ABCD") -> bytes:
    rom = bytearray(0x200)
    rom[0xA0 : 0xA0 + len(title)], rom[0xAC:0xB0], rom[0xB2] = title, code, 0x96
    rom[0xBD] = (-(sum(rom[0xA0:0xBD]) + 0x19)) & 0xFF
    return bytes(rom)


def colour(flag: int) -> bytes:
    rom = bytearray(ACID2.read_bytes())
    rom[0x143] = flag
    rom[0x14D] = 0
    for b in rom[0x134:0x14D]:
        rom[0x14D] = (rom[0x14D] - b - 1) & 0xFF
    return bytes(rom)


def test_a_cartridge_is_recognised_by_its_header_alone():
    acid2 = ACID2.read_bytes()
    assert platform(acid2) == "gb" and platform(gba_header()) == "gba"
    assert platform(colour(0x80)) == platform(colour(0xC0)) == "gbc"
    for broken in (acid2[:0x14D] + b"\x01" + acid2[0x14E:], acid2[:0x111] + b"\0" + acid2[0x112:]):
        assert platform(broken) is None  # a wrong header checksum, a wrong logo
    assert platform(gba_header()[:0xBD] + b"\0" + gba_header()[0xBE:]) is None
    assert platform(b"not a game") is None and platform(bytes(1 << 20)) is None


def test_identify_falls_back_to_the_generic_adapter_and_names_the_game_from_its_header():
    gb = identify(ACID2.read_bytes())
    assert isinstance(gb, Generic) and gb.title == "DMG-ACID2 (Game Boy; no adapter for this game)"
    assert (gb.FAMILY, gb.CORE, gb.CORE_OPTIONS) == ("generic-gb", "sameboy", pokemon_gen1.CORE_OPTIONS)
    assert gb.ROMS == {} and gb.DEFAULT_CHECK is None
    gba = identify(gba_header())
    assert gba.title == "HACKROM ABCD (Game Boy Advance; no adapter for this game)"
    assert (gba.FAMILY, gba.CORE, gba.CORE_OPTIONS) == ("generic-gba", "mgba", pokemon_gen3.CORE_OPTIONS)
    assert "l" in gba.BUTTONS and gba.RAM_RANGES == pokemon_gen3.RAM_RANGES and "l" not in gb.BUTTONS
    assert identify(colour(0xC0)).CORE_OPTIONS == pokemon_gen2.CORE_OPTIONS
    with pytest.raises(ValueError, match="not a GB, GBC or GBA cartridge"):
        identify(b"not a game")


def test_hacks_the_pokemon_adapters_refuse_go_to_the_generic_one(find_rom):
    for name in ("03 Pokemon_ROWE.gba", "04 Pokemon Radical Red 3.0.2.gba"):
        assert type(identify(find_rom(name).read_bytes())) is Generic  # hacks the Gen 3 tables cannot read


def test_a_frame_is_judged_coarsely():
    screen = Image.new("RGB", (160, 144), (100, 100, 100))
    ImageDraw.Draw(screen).rectangle((0, 0, 159, 40), fill=(30, 30, 30))
    blink = screen.copy()
    ImageDraw.Draw(blink).rectangle((100, 100, 103, 103), fill=(0, 0, 0))  # a cursor or sprite frame: less than a cell
    moved = Image.new("RGB", (160, 144), (100, 100, 100))
    ImageDraw.Draw(moved).rectangle((0, 0, 159, 100), fill=(30, 30, 30))  # another screen
    assert frame_id(screen) == frame_id(blink) != frame_id(moved)


@pytest.fixture
def live():
    rom = ACID2.read_bytes()
    with Emulator(core_path("sameboy"), ACID2, options=pokemon_gen1.CORE_OPTIONS) as emu:
        yield Game(emu, identify(rom))


def test_press_reports_with_a_screenshot_and_the_other_macros_refuse(live):
    text, png = live.act("press", button="start", times=2)
    assert text.startswith("press: pressed start x2\nChanged: ")
    assert png and png.startswith(b"\x89PNG")
    for name, args in (("walk", {"direction": "up"}), ("walk_to", {"x": 1, "y": 1}), ("advance_text", {})):
        with pytest.raises(ValueError, match="no adapter for that; use press"):
            live.act(name, **args)
    with pytest.raises(ValueError, match="button must be one of"):
        live.act("press", button="l")  # a Game Boy has no shoulder buttons
    assert "not judged" not in live.state_text() and live.location() == ""
    with pytest.raises(ValueError, match="no text encoding"):
        live.try_observation({"address": 0xC000, "length": 2, "decode": "text"})


def test_novelty_is_progress_on_the_screens_seen_on_the_timeline(live):
    ladder = stuck.Ladder(live.adapter)
    live.run([], 1)
    assert ladder.observe(live.progress_state(), 0) == 0 and ladder.best == 1
    live.run([], 120)  # dmg-acid2 draws its picture
    assert ladder.observe(live.progress_state(), 0) == 0 and ladder.best == 2
    for _ in range(3):
        live.act("press", button="a")  # the picture no longer changes
        assert ladder.observe(live.progress_state(), 0) == 0 and ladder.best == 2
    assert ladder.since == 3
    assert "a screen not seen before" in stuck.describe(1, 40, live.adapter)


class Screens(FakeGame):
    adapter = Generic

    def __init__(self, map_id=0, in_game=True):
        super().__init__()
        self.screen = 0

    def progress_state(self):
        return {"in_game": False, "frame_id": str(self.screen)}

    def state_text(self):
        return NOTE


def test_the_runner_plays_a_game_without_pokemon_fields(tmp_path, monkeypatch):
    monkeypatch.setattr("test_play.FakeGame", Screens)
    runner, briefs, _ = make_runner(tmp_path, [(SessionResult("completed"), None)] * 2, objective="play")
    game, replay = runner.game, runner.provider
    notices = []

    def acting(spec, on_event):
        session = runner.server.sessions[spec.token]
        if session.number == 1:
            for i in range(10):  # a new screen each time
                game.screen = i
                assert runner._after_action() == (0, "")
        while not session.ended and len(notices) < 3:  # pacing between two screens
            game.screen = 1 - game.screen % 2
            climbed, notice = runner._after_action()
            notices.extend([notice] if notice else [])
            session.ended = climbed >= 3
        return replay(spec, on_event)

    runner.provider = acting
    runner.loop("url", max_sessions=2)
    assert not runner.memory.events("milestone", limit=-1)
    assert [n.split("stuck level ")[1][0] for n in notices] == ["1", "2", "3"]
    assert all("a screen not seen before" in n for n in notices)
    assert NOTE in briefs[0] and "Recent milestones" not in briefs[1] and "Stuck: no progress" in briefs[1]


def test_a_generic_run_is_a_direct_run_with_no_default_check_and_no_saves(tmp_path):
    run = play_module.new_run(ACID2, None, tmp_path)
    meta = __import__("json").loads((run / "run.json").read_text())
    assert (meta["style"], meta["variety"], meta["check"], meta["family"]) == ("direct", None, None, "generic-gb")
    assert meta["objective"] == Generic.DEFAULT_OBJECTIVE
    with pytest.raises(ValueError, match="unknown observation"):
        play_module.new_run(ACID2, None, tmp_path, success={"obs": "badges", "op": ">=", "value": 1})
    sav = tmp_path / "x.sav"
    sav.write_bytes(bytes(0x8000))
    with pytest.raises(ValueError, match="no adapter"):
        play_module.new_run(ACID2, None, tmp_path, sav=sav)
    with pytest.raises(ValueError, match="no adapter"):
        identify(ACID2.read_bytes()).save_game(None)  # export-sav
