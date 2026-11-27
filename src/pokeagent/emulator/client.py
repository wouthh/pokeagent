"""Orchestrator-side handle to a sandboxed emulator worker."""

import json
import os
import select
import subprocess
import time
from collections.abc import Iterable, Mapping
from pathlib import Path

from PIL import Image

from pokeagent import sandbox
from pokeagent.emulator.worker import MAX_BLOB, MAX_LINE


class EmulatorError(RuntimeError):
    """The worker refused a request, broke the protocol, timed out or died."""


class Emulator:
    """One core and one ROM in a sandboxed worker process. Not thread-safe: callers serialise access."""

    def __init__(
        self,
        core: Path,
        rom: Path,
        *,
        options: Mapping[str, str] | None = None,
        log: Path | None = None,
        timeout: float = 60,
    ):
        sandbox.ensure()
        self.timeout = timeout
        rom_in = "/in/rom" + rom.suffix.lower()
        binds = {"/in/core.so": core, rom_in: rom}
        args = ["/in/core.so", rom_in, json.dumps(dict(options or {}))]
        stderr = log.open("ab") if log else subprocess.DEVNULL
        try:
            self._proc = sandbox.spawn(
                "pokeagent.emulator.worker",
                args,
                binds,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr,
                bufsize=0,
            )
        finally:
            if log:
                stderr.close()
        self._buf = bytearray()
        self.info = self._call("info")[0]

    def __enter__(self) -> "Emulator":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self._proc.poll() is None:
            self._proc.stdin.close()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                sandbox.kill(self._proc)

    def run(self, buttons: Iterable[str] = (), frames: int = 1) -> None:
        """Hold `buttons` for `frames` frames, then release them."""
        self._call("run", buttons=sorted(buttons), frames=frames)

    def frame(self) -> Image.Image:
        head, data = self._call("frame")
        return Image.frombytes("RGB", (head["width"], head["height"]), data)

    def read(self, addr: int, n: int) -> bytes:
        """Read-only access to RAM regions; ROM is refused."""
        return self._call("read", addr=addr, n_bytes=n)[1]

    def ram_hash(self) -> str:
        """sha256 of system RAM plus all of save RAM (every bank), e.g. to check that a state restores the save."""
        return self._call("ram_hash")[0]["sha256"]

    def save_state(self) -> bytes:
        return self._call("save_state")[1]

    def load_state(self, state: bytes) -> None:
        self._call("load_state", state)

    def _call(self, op: str, blob: bytes = b"", **fields) -> tuple[dict, bytes]:
        if self._proc.poll() is not None:
            raise EmulatorError(f"emulator worker exited with {self._proc.returncode}")
        fields["op"] = op
        if blob:
            fields["n"] = len(blob)
        out = memoryview(json.dumps(fields).encode() + b"\n" + blob)
        deadline = time.monotonic() + self.timeout
        wfd = self._proc.stdin.fileno()
        os.set_blocking(wfd, False)
        try:
            while out:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([], [wfd], [], remaining)[1]:
                    sandbox.kill(self._proc)
                    raise EmulatorError(f"emulator worker did not accept the request within {self.timeout:g}s")
                try:
                    out = out[os.write(wfd, out) :]
                except BlockingIOError:
                    continue
        except BrokenPipeError:
            raise EmulatorError("emulator worker exited") from None
        head = json.loads(self._read_until(lambda: self._buf.find(b"\n") + 1, MAX_LINE, deadline))
        n = head.get("n", 0)
        if not 0 <= n <= MAX_BLOB:
            sandbox.kill(self._proc)
            raise EmulatorError("emulator reply exceeds the size cap")
        data = self._read_until(lambda: n if len(self._buf) >= n else 0, MAX_BLOB, deadline) if n else b""
        if not head.get("ok"):
            raise EmulatorError(head.get("error", "unknown worker error"))
        return head, data

    def _read_until(self, ready, cap: int, deadline: float) -> bytes:
        """Read from the worker until `ready()` returns a byte count to consume; enforce size cap and deadline."""
        fd = self._proc.stdout.fileno()
        while not (count := ready()):
            if len(self._buf) > cap:
                sandbox.kill(self._proc)
                raise EmulatorError("emulator reply exceeds the size cap")
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
                sandbox.kill(self._proc)
                raise EmulatorError(f"emulator worker did not answer within {self.timeout:g}s")
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                raise EmulatorError(f"emulator worker exited with {self._proc.wait()}")
            self._buf += chunk
        out = bytes(self._buf[:count])
        del self._buf[:count]
        return out
