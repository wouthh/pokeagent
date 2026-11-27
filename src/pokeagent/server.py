"""The `game` MCP server that Claude sessions play through: streamable HTTP on 127.0.0.1.

Every HTTP request must carry the bearer token of an open session (checked before MCP sees the request) and this
server's own Host header (DNS-rebinding protection). Tools look the session up by its token, so a token only ever
reaches its own session, budget and mode. Game calls run one at a time under the game's lock, off the event loop.
"""

import hmac
import logging
import secrets
import socket
import threading
import time
from dataclasses import dataclass
from functools import partial
from pathlib import Path

import anyio
import uvicorn
from mcp.server.mcpserver import Context, Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

from pokeagent.emulator.client import EmulatorError

NOTES_LIMIT = 8 * 1024


@dataclass
class Session:
    number: int
    token: str
    budget: int
    mode: str = "play"
    actions: int = 0
    summary: str | None = None
    next_focus: str | None = None
    ended: bool = False


class GameServer:
    """Owns the sessions and the tools. `game` provides `full_state()` and `act(name, **args)`, both returning
    (text, png bytes or None)."""

    def __init__(self, game, notes: Path, harness_issues: Path, run_name: str):
        self.game, self.notes, self.harness_issues, self.run_name = game, notes, harness_issues, run_name
        self.sessions: dict[str, Session] = {}
        self.mcp = MCPServer("game")
        self.after_action = None  # called after every action (the runner's periodic checkpoint)
        self.broken: str | None = None  # set once the emulator fails; game tools then refuse
        self._uvicorn: uvicorn.Server | None = None
        self._register_tools()

    def open_session(self, number: int, budget: int) -> Session:
        session = Session(number, secrets.token_urlsafe(32), budget)
        self.sessions[session.token] = session
        return session

    def close_session(self, session: Session) -> None:
        self.sessions.pop(session.token, None)

    def lookup(self, authorization: str) -> Session | None:
        presented = authorization.removeprefix("Bearer ").encode()
        return next((s for t, s in list(self.sessions.items()) if hmac.compare_digest(t.encode(), presented)), None)

    def start(self) -> str:
        """Serve on a free 127.0.0.1 port in a background thread; return the MCP URL."""
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[f"127.0.0.1:{port}"],
            allowed_origins=[f"http://127.0.0.1:{port}"],
        )
        logging.getLogger("mcp").setLevel(logging.WARNING)  # quiet: no per-session INFO lines on the terminal
        app = _RequireSession(self.mcp.streamable_http_app(transport_security=security), self.lookup)
        self._uvicorn = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
        thread = threading.Thread(target=self._uvicorn.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        while not self._uvicorn.started:
            if not thread.is_alive():
                raise RuntimeError("the MCP server failed to start")
            time.sleep(0.01)
        self._thread = thread
        return f"http://127.0.0.1:{port}/mcp"

    def stop(self) -> None:
        if self._uvicorn:
            self._uvicorn.should_exit = True
            self._thread.join(10)

    def _session(self, ctx: Context) -> Session:
        session = self.lookup((ctx.headers or {}).get("authorization", ""))
        if session is None:
            raise ToolError("unknown session")
        return session

    def _register_tools(self) -> None:
        tool = self.mcp.tool(structured_output=False)

        async def on_game(fn):
            if self.broken:
                raise ToolError(f"the emulator has stopped ({self.broken}); end the session")
            try:
                return await anyio.to_thread.run_sync(fn)
            except ValueError as e:
                raise ToolError(str(e)) from e
            except EmulatorError as e:
                self.broken = str(e)
                raise ToolError(f"the emulator has stopped ({e}); end the session") from e

        async def act(ctx: Context, name: str, **args):
            session = self._session(ctx)
            if session.ended:
                raise ToolError("this session has ended; stop now")
            if session.actions >= session.budget:
                raise ToolError(f"the action budget ({session.budget}) is used up: update your notes, then end_session")
            session.actions += 1
            text, png = await on_game(partial(self.game.act, name, **args))
            if self.after_action:
                await on_game(self.after_action)
            return [text, Image(data=png, format="png")] if png else text

        @tool
        async def get_state(ctx: Context) -> list:
            """The full game state: map and position, screen mode and text, party, bag, money, badges, the whole
            current map as ASCII with a legend, and a screenshot. Free: it does not use the action budget."""
            self._session(ctx)
            text, png = await on_game(self.game.full_state)
            return [text, Image(data=png, format="png")]

        @tool
        async def press(button: str, ctx: Context, times: int = 1) -> list | str:
            """Press a button (a, b, start, select, up, down, left, right) `times` times (1-10), waiting briefly after
            each press. One action."""
            return await act(ctx, "press", button=button, times=times)

        @tool
        async def walk(direction: str, ctx: Context, steps: int = 1) -> list | str:
            """Walk up to `steps` steps (1-20) in a direction (up, down, left, right). Stops early when blocked, on a
            map change, a battle or a dialog. One action."""
            return await act(ctx, "walk", direction=direction, steps=steps)

        @tool
        async def walk_to(x: int, y: int, ctx: Context) -> list | str:
            """Walk to (x, y) on the current map along a shortest path (x grows to the right, y downwards, one unit
            per step, as in get_state's map). Stops on a map change, a battle or a dialog. One action."""
            return await act(ctx, "walk_to", x=x, y=y)

        @tool
        async def advance_text(ctx: Context) -> list | str:
            """Press A through the current dialog until it closes. Stops without pressing anything when a menu or
            yes/no prompt appears, so you make every choice yourself. One action."""
            return await act(ctx, "advance_text")

        @tool
        def notes_read(ctx: Context) -> str:
            """Your notes, kept across sessions."""
            self._session(ctx)
            return self.notes.read_text() if self.notes.exists() else "(empty)"

        @tool
        def notes_write(text: str, ctx: Context) -> str:
            """Replace your notes (at most 8 KB). They are shown at the start of every session."""
            self._session(ctx)
            if len(text.encode()) > NOTES_LIMIT:
                raise ToolError(f"notes are limited to {NOTES_LIMIT} bytes; shorten them")
            tmp = self.notes.with_suffix(".tmp")
            tmp.write_text(text)
            tmp.replace(self.notes)
            return "saved"

        @tool
        def end_session(summary: str, next_focus: str, ctx: Context) -> str:
            """End this session: what you did and learned, and what the next session should focus on."""
            session = self._session(ctx)
            session.summary, session.next_focus, session.ended = summary[:4000], next_focus[:1000], True
            return "Recorded. This session is over: stop now, without further tool calls."

        @tool
        def report_harness_issue(description: str, evidence: str, ctx: Context) -> str:
            """Report a problem with pokeagent itself (a tool that misbehaves or reports wrong state), not with the
            game. The user reads these reports."""
            session = self._session(ctx)
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            heading = f"## {stamp}, run {self.run_name}, session {session.number}"
            entry = f"\n{heading}\n\n{description}\n\nEvidence: {evidence}\n"
            with self.harness_issues.open("a") as f:
                f.write(entry[:8000])
            return "recorded"


class _RequireSession:
    """ASGI wrapper: HTTP requests without an open session's bearer token get 401 before MCP sees them."""

    def __init__(self, app, lookup):
        self.app, self.lookup = app, lookup

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            authorization = dict(scope["headers"]).get(b"authorization", b"").decode("latin-1")
            if self.lookup(authorization) is None:
                headers = [(b"content-type", b"text/plain")]
                await send({"type": "http.response.start", "status": 401, "headers": headers})
                await send({"type": "http.response.body", "body": b"unknown session\n"})
                return
        await self.app(scope, receive, send)
