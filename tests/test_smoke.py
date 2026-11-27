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


def test_find_run_path_resolves_owning_data_dir(tmp_path):
    run = tmp_path / "data" / "runs" / "r1"
    run.mkdir(parents=True)
    (run / "run.json").write_text("{}")
    assert cli.find_run(str(run), None) == (run.resolve(), (tmp_path / "data").resolve())
    assert cli.find_run("r1", tmp_path / "data") == (tmp_path / "data" / "runs" / "r1", tmp_path / "data")
