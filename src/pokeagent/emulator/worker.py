"""Emulator worker: one libretro core per process (cores keep global state), run inside the sandbox.

Protocol on stdin/stdout: every message is one JSON line; when it has `"n": N`, N raw bytes follow. Requests carry
`op`; replies carry `ok` and either the result fields or `error`. The core prints to stdout on its own, so fd 1 is
redirected to stderr and replies go to a private duplicate of the original stdout.

There is deliberately no memory-write operation: progress happens only through controller inputs.
"""

import hashlib
import json
import os
import sys

MAX_LINE = 1 << 20
MAX_BLOB = 16 << 20
BUTTONS = frozenset({"a", "b", "select", "start", "up", "down", "left", "right", "l", "r", "x", "y"})


def serve(core: str, rom: str, options: dict[str, str]) -> None:
    rpc_out = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    rpc_in = sys.stdin.buffer

    from libretro import ArrayVideoDriver, DictOptionDriver, IterableInputDriver, Session
    from libretro.api import MemoryDescriptorFlag
    from libretro.api.input import JoypadState
    from PIL import Image

    held = [JoypadState()]

    def inputs():
        while True:
            yield held[0]

    def reply(blob: bytes = b"", **fields) -> None:
        if blob:
            fields["n"] = len(blob)
        rpc_out.write(json.dumps(fields).encode() + b"\n" + blob)
        rpc_out.flush()

    with Session(
        core,
        rom,
        input=IterableInputDriver(inputs),
        video=ArrayVideoDriver,
        options=DictOptionDriver(variables=options),
        log=None,
    ) as session:
        # Readable regions: every non-const memory descriptor (RAM, VRAM, OAM, IO); ROM is const and excluded.
        regions = [
            d
            for d in (session.memory_maps or [])
            if d.len and d.ptr is not None and not d.flags & MemoryDescriptorFlag.CONST
        ]

        def read(addr: int, n: int) -> bytes:
            if not 0 < n <= 0x10000:
                raise ValueError("n must be 1..65536")
            out = bytearray()
            while len(out) < n:
                a = addr + len(out)
                region = next((d for d in regions if d.start <= a < d.start + d.len), None)
                if region is None:
                    raise ValueError(f"address {a:#06x} is not readable RAM")
                take = min(n - len(out), region.start + region.len - a)
                out += region.view[a - region.start : a - region.start + take]
            return bytes(out)

        def ram_hash() -> str:
            h = hashlib.sha256()
            for mem_id in (2, 0):  # RETRO_MEMORY_SYSTEM_RAM, RETRO_MEMORY_SAVE_RAM
                view = session.core.get_memory(mem_id)
                if view is not None:
                    h.update(view)
            return h.hexdigest()

        av = session.core.get_system_av_info()
        info = session.core.get_system_info()
        while True:
            line = rpc_in.readline(MAX_LINE + 1)
            if not line:
                return
            if len(line) > MAX_LINE or not line.endswith(b"\n"):
                sys.exit("request line too long")
            req = json.loads(line)
            n = req.get("n", 0)
            if not 0 <= n <= MAX_BLOB:
                sys.exit("request blob too large")
            blob = rpc_in.read(n) if n else b""
            try:
                match req["op"]:
                    case "info":
                        reply(
                            ok=True,
                            library=info.library_name.decode(),
                            version=info.library_version.decode(),
                            fps=av.timing.fps,
                            width=av.geometry.base_width,
                            height=av.geometry.base_height,
                            state_size=session.core.serialize_size(),
                        )
                    case "run":
                        buttons = set(req["buttons"])
                        if not buttons <= BUTTONS:
                            raise ValueError(f"unknown buttons: {sorted(buttons - BUTTONS)}")
                        frames = int(req["frames"])
                        if not 1 <= frames <= 60 * 60:
                            raise ValueError("frames must be 1..3600")
                        held[0] = JoypadState(**dict.fromkeys(buttons, True))
                        for _ in range(frames):
                            session.run()
                        held[0] = JoypadState()
                        reply(ok=True)
                    case "frame":
                        shot = session.video.screenshot()
                        size = (shot.width, shot.height)
                        img = Image.frombuffer("RGBA", size, bytes(shot.data), "raw", "BGRA", 0, 1)
                        reply(img.convert("RGB").tobytes(), ok=True, width=shot.width, height=shot.height)
                    case "read":
                        reply(read(int(req["addr"]), int(req["n_bytes"])), ok=True)
                    case "ram_hash":
                        reply(ok=True, sha256=ram_hash())
                    case "save_state":
                        state = bytearray(session.core.serialize_size())
                        if not session.core.serialize(state):
                            raise RuntimeError("core failed to serialize")
                        reply(bytes(state), ok=True)
                    case "load_state":
                        if len(blob) != session.core.serialize_size():
                            raise ValueError("state size does not match this core and ROM")
                        if not session.core.unserialize(blob):
                            raise RuntimeError("core rejected the state")
                        reply(ok=True)
                    case op:
                        raise ValueError(f"unknown op {op!r}")
            except (KeyError, TypeError, ValueError, RuntimeError) as e:
                reply(ok=False, error=f"{type(e).__name__}: {e}")


if __name__ == "__main__":
    serve(sys.argv[1], sys.argv[2], json.loads(sys.argv[3]))
