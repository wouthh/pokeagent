import json
from pathlib import Path

import pytest

from pokeagent import sandbox

ESCAPE = """
import json, os, resource, socket, sys, time
r = {}
def attempt(name, fn):
    try:
        fn(); r[name] = "allowed"
    except (OSError, MemoryError, ValueError):
        r[name] = "blocked"
attempt("network", lambda: socket.create_connection(("127.0.0.1", 1), timeout=1))
attempt("read_outside", lambda: open(sys.argv[1]).read())
attempt("read_repo", lambda: open(sys.argv[2]).read())
attempt("write_mount", lambda: open("/work/x", "w").write("x"))
attempt("write_tmp", lambda: open("/tmp/x", "w").write("x"))
attempt("raise_limit", lambda: resource.setrlimit(resource.RLIMIT_NPROC, (10**6, 10**6)))
attempt("big_alloc", lambda: bytearray(1 << 31))
r["home"] = os.environ.get("HOME")
r["env"] = sorted(k for k in os.environ if k not in ("HOME", "LANG", "PWD", "LC_CTYPE"))
print(json.dumps(r))
"""


def _run(tmp_path: Path, code: str, *args: str, timeout: float = 30, **limits) -> object:
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    (work / "main.py").write_text(code)
    lim = {**sandbox.WORKER_LIMITS, **limits}
    return sandbox.run("/work/main.py", list(args), {"/work": work}, lim, timeout, script=True)


def test_probe_passes():
    sandbox.ensure()


def test_escape_attempts_are_blocked(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("private")
    readme = Path(__file__).resolve().parent.parent / "README.md"  # next to the mounted src/ and .venv/
    result = _run(tmp_path, ESCAPE, str(secret), str(readme))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "network": "blocked",
        "read_outside": "blocked",
        "read_repo": "blocked",
        "write_mount": "blocked",
        "write_tmp": "allowed",
        "raise_limit": "blocked",
        "big_alloc": "blocked",
        "home": "/tmp",
        "env": [],
    }


def test_process_limit(tmp_path):
    code = """
import os, time
n = 0
try:
    while True:
        if os.fork() == 0:
            time.sleep(3)
            os._exit(0)
        n += 1
except OSError:
    print(n)
"""
    result = _run(tmp_path, code, RLIMIT_NPROC=8)
    assert result.returncode == 0, result.stderr
    assert int(result.stdout) < 8


def test_cpu_limit_kills(tmp_path):
    result = _run(tmp_path, "while True: pass\n", RLIMIT_CPU=1)
    assert result.returncode != 0


def test_wall_timeout_kills_process_group(tmp_path):
    code = "import os, time\nif os.fork() == 0:\n    time.sleep(60)\ntime.sleep(60)\n"
    with pytest.raises(sandbox.SandboxError, match="killed"):
        _run(tmp_path, code, timeout=1)


def test_probe_failure_fails_closed(monkeypatch):
    sandbox.ensure.cache_clear()
    real_run = sandbox.run

    def fake_run(*args, **kwargs):
        result = real_run(*args, **kwargs)
        result.stdout = json.dumps({"network": "open"})
        return result

    monkeypatch.setattr(sandbox, "run", fake_run)
    with pytest.raises(sandbox.SandboxError, match="probe failed"):
        sandbox.ensure()
    sandbox.ensure.cache_clear()
