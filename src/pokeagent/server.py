"""The `game` MCP server that Claude sessions play through: streamable HTTP on 127.0.0.1.

Every HTTP request must carry the bearer token of an open session (checked before MCP sees the request) and this
server's own Host header (DNS-rebinding protection). Tools look the session up by its token, so a token only ever
reaches its own session, budget and mode. Game calls run one at a time under the game's lock, off the event loop.
Every action is recorded as an event; its id is the evidence the agent cites when it confirms knowledge.
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

from pokeagent import capabilities, objectives
from pokeagent.emulator.client import EmulatorError
from pokeagent.memory import Memory, describe_knowledge

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
    claim: str | None = None
    ended: bool = False


class GameServer:
    """Owns the sessions and the tools. `game` provides `full_state()` and `act(name, **args)`, both returning
    (text, png bytes or None), `observations()` (the builtin ones), `registry` (the agent's capabilities),
    `run_macro(name, args)`, `run_tool(name, args)`, `try_observation(spec)` and `peek(address, length)`."""

    def __init__(self, game, memory: Memory, notes: Path, harness_issues: Path):
        self.game, self.memory, self.notes, self.harness_issues = game, memory, notes, harness_issues
        self.sessions: dict[str, Session] = {}
        self.mcp = MCPServer("game")
        self.after_action = None  # called after every action (the runner's recovery state and checkpoints)
        self.recover = None  # called with the error when the emulator fails; True once it runs again
        self.broken: str | None = None  # set once the emulator fails for good; game tools then refuse
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
                try:
                    restarted = self.recover is not None and await anyio.to_thread.run_sync(self.recover, str(e))
                except EmulatorError:
                    restarted = False
                if restarted:
                    raise ToolError(
                        f"the emulator stopped during this call ({e}) and was restarted at the state after your last"
                        " completed action, so this call had no effect; check get_state"
                    ) from e
                self.broken = str(e)
                raise ToolError(f"the emulator has stopped ({e}); end the session") from e

        async def act(ctx: Context, name: str, call=None, **args):
            session = self._session(ctx)
            if session.ended:
                raise ToolError("this session has ended; stop now")
            if session.actions >= session.budget:
                raise ToolError(f"the action budget ({session.budget}) is used up: update your notes, then end_session")
            session.actions += 1
            text, png = await on_game(call or partial(self.game.act, name, **args))
            if self.after_action:  # an action whose recovery state was not saved is undone by a restart: no event
                await on_game(self.after_action)
            outcome = {"name": name, "args": args, "outcome": text.split("\n", 1)[0]}
            text = f"[event {self.memory.event('action', outcome, session.number)}] {text}"
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
            heading = f"## {stamp}, run {self.memory.run}, session {session.number}"
            entry = f"\n{heading}\n\n{description}\n\nEvidence: {evidence}\n"
            with self.harness_issues.open("a") as f:
                f.write(entry[:8000])
            return "recorded"

        def remember(fn, *args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except ValueError as e:
                raise ToolError(str(e)) from e

        @tool
        def knowledge_search(query: str, ctx: Context, limit: int = 5) -> str:
            """Search what was learned in this run, earlier runs on this ROM and this game family, and in general.
            Matches any of the query's words; refuted and superseded entries are left out."""
            self._session(ctx)
            found = remember(self.memory.search, query, max(1, min(limit, 20)))
            return "\n".join(describe_knowledge(entry) for entry in found) or "nothing found"

        @tool
        def knowledge_add(
            kind: str, topic: str, content: str, ctx: Context, scope: str = "rom", url: str = "", evidence: int = 0
        ) -> str:
            """Record something learned, for later sessions and later runs. kind: mechanic, technical, map, strategy,
            lesson, limitation or progress (what this run's game has done; scope run). scope: run, rom, family or
            general. It is a hypothesis unless `evidence` names an action event (the [event N] of an action result in
            this game) that showed it; anything from the web (give its url) stays a hypothesis until confirmed."""
            session = self._session(ctx)
            entry = remember(self.memory.add_knowledge, kind, topic, content, scope, url, evidence, session.number)
            return f"recorded as #{entry}"

        @tool
        def knowledge_set_status(
            entry: int, status: str, ctx: Context, evidence: int = 0, superseded_by: int = 0
        ) -> str:
            """Mark a knowledge entry hypothesis, confirmed (needs `evidence`: an action event id from this game),
            refuted or stale, or name the entry that replaces it in `superseded_by`."""
            self._session(ctx)
            remember(self.memory.set_knowledge_status, entry, status, evidence, superseded_by)
            return "updated"

        async def taken(kind: str) -> set[str]:
            """Names a new capability of `kind` may not take: the builtin ones and the other kind's."""
            builtin = set(await on_game(self.game.observations)) | set(capabilities.ACTIONS) | {"run_macro"}
            return builtin | {c["name"] for c in self.game.registry.active() if c["kind"] != kind}

        @tool
        async def mem_read(address: int, ctx: Context, length: int = 1) -> str:
            """Read `length` (1-64) bytes of the game's memory from `address`, as hex: work RAM C000-DFFF, high RAM
            FF80-FFFE, cartridge RAM A000-BFFF and the other RAM regions, never ROM. Read-only and free."""
            self._session(ctx)
            if not 1 <= length <= capabilities.MAX_READ:
                raise ToolError(f"length must be 1..{capabilities.MAX_READ}")
            data = await on_game(partial(self.game.peek, address, length))
            return f"{address:#06x}: {data.hex(' ')}"

        @tool
        async def define_observation(
            name: str,
            description: str,
            address: int,
            ctx: Context,
            length: int = 1,
            decode: str = "u8",
            bit: int | None = None,
            show: bool = True,
            scope: str = "rom",
            expect: dict | None = None,
        ) -> str:
            """Define (or redefine, as a new version) a named value read from RAM, kept for this run and later runs on
            this ROM (scope rom) or game family (scope family). decode: u8, u16le, u16be, bcd, bit (with `bit` 0-7),
            popcount, bytes or text. Shown in get_state when `show`, and usable in macro conditions. `expect`, such as
            {"op": "==", "value": 5}, must hold for the value read now, or the definition is refused. Free."""
            session = self._session(ctx)
            spec = {"name": name, "description": description, "address": address, "length": length}
            spec |= {"decode": decode, "show": show} | ({} if bit is None else {"bit": bit})
            spec = remember(capabilities.check_observation, spec, await taken("observation"))
            value = await on_game(partial(self.game.try_observation, spec))
            if expect is not None:
                condition = {"obs": name, **expect} if isinstance(expect, dict) else expect
                remember(objectives.validate, condition, [name])
                if not objectives.holds(condition, {name: value}):
                    raise ToolError(
                        f"not defined: {name} reads {value!r} now, which fails {objectives.describe(condition)}"
                    )
            found = remember(self.game.registry.define, "observation", spec, scope, session.number)
            return f"defined {name} version {found['version']}; it reads {value!r} now"

        @tool
        async def define_macro(
            name: str, description: str, steps: list, ctx: Context, params: dict | None = None, scope: str = "rom"
        ) -> str:
            """Define (or redefine, as a new version) a named sequence of actions, kept like observations. Each step is
            {"do": "press" | "walk" | "walk_to" | "advance_text" | "macro", "args": {...}} with the action's own
            arguments (a macro step: {"name": ..., "args": {...}}), plus optionally "repeat": n, or "until" / "while":
            condition with "max": n. Conditions use the grammar of propose_success_check over builtin observations and
            yours. `params` is a JSON schema for run_macro's args; {"$arg": "x"} in a step's args stands for arg x.
            A call runs at most 200 actions and 4 nested macros. Free; it is not run now."""
            session = self._session(ctx)
            spec = {"name": name, "description": description, "steps": steps} | ({"params": params} if params else {})
            macros = {c["name"] for c in self.game.registry.active("macro")} | {name}
            names = set(await on_game(self.game.observations)) | {
                c["name"] for c in self.game.registry.active("observation")
            }
            spec = remember(capabilities.check_macro, spec, names, macros, await taken("macro"))
            found = remember(self.game.registry.define, "macro", spec, scope, session.number)
            return f"defined {name} version {found['version']}; run it with run_macro"

        async def run_capability(ctx: Context, run, name: str, args: dict | None):
            ran = {}

            def call():
                text, png, ran["capability"], ran["error"] = run(name, args or {})
                return text, png

            reply = await act(ctx, run.__name__, call, capability=name, args=args or {})
            if ran and (note := self.game.registry.record(ran["capability"], ran["error"], self._session(ctx).number)):
                return [*reply, note] if isinstance(reply, list) else f"{reply}\n{note}"
            return reply

        @tool
        async def run_macro(name: str, ctx: Context, args: dict | None = None) -> list | str:
            """Run one of your macros with `args`. One action, however many actions the macro takes. A macro that
            fails part way stops there and reports why; a version that keeps failing is rolled back."""
            return await run_capability(ctx, self.game.run_macro, name, args)

        @tool
        async def run_tool(name: str, ctx: Context, args: dict | None = None) -> list | str:
            """Run one of your code tools (listed in your brief and by list_capabilities) with `args`. One action. A
            tool runs sandboxed with a budget and is held to its declared contract; a call it breaks counts as a
            failure, and a version that keeps failing is rolled back."""
            return await run_capability(ctx, self.game.run_tool, name, args)

        @tool
        def list_capabilities(ctx: Context) -> str:
            """Your observations, macros and tools (this ROM's and this game family's), with their use so far."""
            self._session(ctx)
            found = self.game.registry.active()
            return "\n".join(capabilities.summary(self.game.registry, c) for c in found) or "none defined yet"

        @tool
        async def propose_success_check(condition: dict, reason: str, ctx: Context) -> str:
            """Propose a check that tells when the objective is met, for objectives without an authoritative one.
            condition: {"obs": name, "op": "==", "!=", ">=", ">", "<=" or "<", "value": number}, or {"all": [...]},
            {"any": [...]} or {"not": condition}, over the observations listed with their current values in your brief.
            When it holds after a session, the run pauses until the user confirms; it never completes the run alone."""
            session = self._session(ctx)
            observations = await on_game(self.game.observations)
            remember(objectives.validate, condition, [name for name in observations if name != "in_game"])
            check = self.memory.add_check(condition, "agent", reason[:1000])
            self.memory.event("check_proposed", {"check": check, "condition": condition}, session.number)
            now = objectives.holds(condition, observations) and observations["in_game"]
            return (
                f"recorded as check #{check} ({objectives.describe(condition)}); it {'holds' if now else 'fails'} now"
            )

        @tool
        def report_objective_complete(evidence: str, ctx: Context) -> str:
            """Claim the objective is met when no success check can show it. The run pauses after this session until the
            user confirms or rejects the claim."""
            session = self._session(ctx)
            if active := self.memory.checks("active"):
                checks = "; ".join(objectives.describe(c["condition"]) for c in active)
                return (
                    f"Not recorded: pokeagent checks {checks} after every session and completes the run when it holds."
                )
            session.claim = evidence[:2000]
            return "Recorded. The run pauses after this session for the user to confirm; end the session now."


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
