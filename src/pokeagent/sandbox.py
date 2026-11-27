"""bubblewrap sandbox for emulator workers and agent-written code.

Everything runs with `--unshare-all` (no network), a cleared environment, read-only system, Python and package
mounts, a tmpfs `/tmp`, and rlimits applied inside the namespace by the trusted bootstrap below. A pre-bwrap
RLIMIT_NPROC would count all of the user's processes and stop bwrap from creating its namespaces.
"""

import contextlib
import functools
import json
import os
import shutil
import signal
import site
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path

import pokeagent


class SandboxError(RuntimeError):
    """The sandbox cannot be established or a sandboxed process misbehaved. Nothing may run (fail closed)."""


# rlimit name -> value; None leaves the limit unset. NPROC counts threads too.
WORKER_LIMITS = {"RLIMIT_AS": 2 << 30, "RLIMIT_NPROC": 32, "RLIMIT_FSIZE": 64 << 20, "RLIMIT_NOFILE": 256}

_BOOT = """
import json, resource, runpy, site, sys
cfg = json.loads(sys.argv[1])
for name, value in cfg["limits"].items():
    resource.setrlimit(getattr(resource, name), (value, value))
for path in cfg["paths"]:
    site.addsitedir(path)
sys.argv = [cfg["target"], *cfg["args"]]
if cfg["kind"] == "module":
    runpy.run_module(cfg["target"], run_name="__main__", alter_sys=True)
else:
    runpy.run_path(cfg["target"], run_name="__main__")
"""


def _python_paths() -> list[str]:
    """sys.path entries the sandboxed interpreter needs: site-packages and the package's parent directory."""
    paths = [str(Path(p).resolve()) for p in site.getsitepackages() if Path(p).is_dir()]
    package_parent = str(Path(pokeagent.__file__).resolve().parent.parent)
    return list(dict.fromkeys([*paths, package_parent]))


def bwrap_argv(binds: Mapping[str, Path]) -> list[str]:
    """bwrap arguments; `binds` maps sandbox path -> host path, all read-only."""
    argv = ["bwrap", "--unshare-all", "--die-with-parent", "--new-session", "--clearenv"]
    argv += ["--ro-bind", "/usr", "/usr", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    for top in ("/bin", "/sbin", "/lib", "/lib32", "/lib64"):
        if os.path.islink(top):
            argv += ["--symlink", os.readlink(top), top]
        elif os.path.isdir(top):
            argv += ["--ro-bind", top, top]
    for path in dict.fromkeys([str(Path(sys.base_prefix).resolve()), *_python_paths()]):
        argv += ["--ro-bind", path, path]
    for dst, src in binds.items():
        argv += ["--ro-bind", str(src), dst]
    argv += ["--setenv", "HOME", "/tmp", "--setenv", "LANG", "C.UTF-8", "--chdir", "/tmp"]
    return argv


def spawn(
    target: str,
    args: list[str],
    binds: Mapping[str, Path],
    limits: Mapping[str, int] = WORKER_LIMITS,
    *,
    script: bool = False,
    **popen,
) -> subprocess.Popen:
    """Start `python -m target` (or the script at sandbox path `target`) inside the sandbox.

    Popen runs it in its own session, so `kill` can terminate the whole process group. bwrap's --die-with-parent
    ties the sandbox to the spawning thread: spawn from a thread that outlives the process.
    """
    if shutil.which("bwrap") is None:
        raise SandboxError("bubblewrap (bwrap) is not installed")
    cfg = {"kind": "script" if script else "module", "target": target, "args": args, "limits": dict(limits)}
    cfg["paths"] = _python_paths()
    python = str(Path(sys.executable).resolve())
    argv = [*bwrap_argv(binds), "--", python, "-I", "-c", _BOOT, json.dumps(cfg)]
    return subprocess.Popen(argv, start_new_session=True, **popen)


def kill(proc: subprocess.Popen) -> None:
    """SIGKILL the sandbox's process group and reap it."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)
    proc.wait()


def run(
    target: str,
    args: list[str],
    binds: Mapping[str, Path],
    limits: Mapping[str, int],
    timeout: float,
    *,
    script: bool = False,
) -> subprocess.CompletedProcess:
    """Run to completion with a wall-clock timeout; on timeout the process group is killed."""
    proc = spawn(
        target,
        args,
        binds,
        limits,
        script=script,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill(proc)
        raise SandboxError(f"sandboxed {target} exceeded {timeout:g}s and was killed") from None
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


@functools.cache
def ensure() -> None:
    """Fail closed unless a probe inside the sandbox can neither reach the network nor read outside its mounts."""
    # The canary lives under the home cache, not /tmp (which the sandbox replaces with its own tmpfs anyway).
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    cache.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pokeagent-probe-", dir=cache) as tmp:
        canary = Path(tmp) / "canary"
        canary.write_text("outside")
        try:
            result = run("pokeagent.sandbox", ["probe", str(canary)], {}, WORKER_LIMITS, timeout=30)
        except OSError as e:
            raise SandboxError(f"cannot start bwrap: {e}") from e
    expected = {
        "network": "blocked",
        "canary": "absent",
        "package_write": "blocked",
        "nproc": WORKER_LIMITS["RLIMIT_NPROC"],
    }
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError:
        report = None
    if result.returncode != 0 or report != expected:
        raise SandboxError(f"sandbox probe failed: {report or result.stderr.strip()[-500:]}")


def _probe(canary: str) -> dict:
    import resource
    import socket

    report = {}
    try:
        socket.create_connection(("1.1.1.1", 53), timeout=2).close()
        report["network"] = "open"
    except OSError:
        report["network"] = "blocked"
    report["canary"] = "readable" if os.path.exists(canary) else "absent"
    try:
        (Path(pokeagent.__file__).parent / ".probe").write_text("x")
        report["package_write"] = "open"
    except OSError:
        report["package_write"] = "blocked"
    report["nproc"] = resource.getrlimit(resource.RLIMIT_NPROC)[0]
    return report


if __name__ == "__main__" and sys.argv[1:2] == ["probe"]:
    print(json.dumps(_probe(sys.argv[2])))
