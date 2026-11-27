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
    result = subprocess.run(
        [sys.executable, "-m", "pokeagent", "--help"], capture_output=True, text=True, check=True
    )
    assert "usage: pokeagent" in result.stdout
