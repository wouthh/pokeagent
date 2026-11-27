"""Runs one code extension inside the sandbox: loads /ext/tool.py and calls its `run(ctx, **params)`.

Everything the extension does to the game goes through `ctx`, whose methods are requests to the orchestrator: one
JSON line out on the protocol pipe, one JSON line back. The extension shares this process, so nothing here is a
boundary: the orchestrator treats every line as untrusted and checks each request itself.

Protocol, host -> orchestrator: {"call": method, "args": {...}} per ctx request, then one final
{"done": true, "result": ...} or {"done": false, "error": "..."}. Orchestrator -> host: {"ok": true, "result": ...}
or {"ok": false, "error": "..."}.
"""

import json
import os
import runpy
import sys

API_VERSION = 1


class CtxError(Exception):
    """The orchestrator refused a ctx request (budget, a bad argument, a failed nested call)."""


class _Store:
    """A small JSON key-value store: ctx.kv is durable, ctx.tkv follows the game's timeline."""

    def __init__(self, request, name: str):
        self._request, self._name = request, name

    def get(self, key: str, default=None):
        found = self._request(f"{self._name}_get", key=key)
        return default if found is None else found["value"]

    def set(self, key: str, value) -> None:
        self._request(f"{self._name}_set", key=key, value=value)

    def delete(self, key: str) -> None:
        self._request(f"{self._name}_set", key=key, value=None)


class Ctx:
    """The extension's only access to the game: read-only RAM and state, controller input and other capabilities."""

    api_version = API_VERSION

    def __init__(self, request):
        self._request = request
        self.kv, self.tkv = _Store(request, "kv"), _Store(request, "tkv")

    def state(self) -> dict:
        """The adapter's state: map, position, party, bag, money, battle, screen."""
        return self._request("state")

    def observe(self, name: str):
        """One observation's value: a builtin one or one the agent defined."""
        return self._request("observe", name=name)

    def read(self, address: int, length: int = 1) -> bytes:
        """Read RAM (never ROM), at most 64 bytes."""
        return bytes.fromhex(self._request("read", address=address, length=length))

    def press(self, button: str, times: int = 1) -> None:
        """Press a button and let the game settle, `times` times."""
        self._request("press", button=button, times=times)

    def hold(self, buttons: list[str], frames: int) -> None:
        """Hold buttons for `frames` frames, then release them."""
        self._request("hold", buttons=list(buttons), frames=frames)

    def wait(self, frames: int) -> None:
        """Let `frames` frames pass without input."""
        self._request("wait", frames=frames)

    def screen_text(self) -> list[str]:
        return self._request("screen_text")

    def call(self, name: str, **args):
        """Run a builtin action (press, walk, walk_to, advance_text), a macro or another tool."""
        return self._request("call", name=name, args=args)

    def log(self, message: str) -> None:
        self._request("log", message=str(message))


def main() -> None:
    proto = os.fdopen(os.dup(1), "w", buffering=1)
    os.dup2(2, 1)  # print() in the extension goes to stderr, not into the protocol
    sys.stdout = sys.stderr

    def send(message: dict) -> None:
        proto.write(json.dumps(message) + "\n")

    def request(method: str, **args):
        send({"call": method, "args": args})
        reply = json.loads(sys.stdin.readline() or '{"ok": false, "error": "the orchestrator went away"}')
        if not reply["ok"]:
            raise CtxError(reply["error"])
        return reply.get("result")

    try:
        run = runpy.run_path("/ext/tool.py")["run"]
        result = run(Ctx(request), **json.loads(sys.argv[1]))
        send({"done": True, "result": result})
    except BaseException as e:  # the extension's own failure, reported as its outcome
        send({"done": False, "error": f"{type(e).__name__}: {e}"[:2000]})


if __name__ == "__main__":
    main()
