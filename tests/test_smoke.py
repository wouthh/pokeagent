import subprocess
import sys

import pytest

from pokeagent import __version__, cli


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_console_script_help():
    result = subprocess.run([sys.executable, "-m", "pokeagent", "--help"], capture_output=True, text=True, check=True)
    assert "usage: pokeagent" in result.stdout


def test_play_needs_a_rom_or_a_run_to_start_from(capsys):
    assert cli.main(["play"]) == 1 and "either --rom" in capsys.readouterr().err
    assert cli.main(["play", "--rom", "x.gb", "--from", "r1"]) == 1 and "either --rom" in capsys.readouterr().err


def test_find_run_path_resolves_owning_data_dir(tmp_path):
    run = tmp_path / "data" / "runs" / "r1"
    run.mkdir(parents=True)
    (run / "run.json").write_text("{}")
    assert cli.find_run(str(run), None) == (run.resolve(), (tmp_path / "data").resolve())
    assert cli.find_run("r1", tmp_path / "data") == (tmp_path / "data" / "runs" / "r1", tmp_path / "data")


def test_capability_list_and_rollback(tmp_path, capsys):
    from pokeagent import capabilities, play

    run = tmp_path / "runs" / "r1"
    run.mkdir(parents=True)
    (run / "run.json").write_text('{"objective": "x", "rom_sha1": "red", "family": "pokemon-gen1"}')
    memory = play.open_memory(run, tmp_path)
    spec = capabilities.check_macro(
        {"name": "spin", "description": "turn", "steps": [{"do": "walk"}]}, set(), set(), set()
    )
    registry = capabilities.Registry(memory)
    registry.define("macro", spec, "rom", 1)
    registry.define("macro", spec | {"description": "turn twice"}, "rom", 2)
    memory.close()
    dirs = ["--data-dir", str(tmp_path)]
    assert cli.main(["capability", "list", "r1", *dirs]) == 0
    assert "spin (macro v2, rom; no params; unused): turn twice" in capsys.readouterr().out
    assert cli.main(["capability", "rollback", "r1", "spin", *dirs]) == 0
    assert "spin version 2 was rolled back (by the user); version 1 is active again" in capsys.readouterr().out
    assert cli.main(["capability", "rollback", "r1", "fly", *dirs]) == 1


def test_the_game_window_is_on_unless_turned_off():
    parser = cli.build_parser()
    assert parser.parse_args(["play", "--rom", "r.gb"]).watch
    assert not parser.parse_args(["play", "--rom", "r.gb", "--no-watch"]).watch
    assert not parser.parse_args(["resume", "r1", "--no-watch"]).watch
