import asyncio
import io
import urllib.error
import urllib.request

import httpx2
import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from PIL import Image

from pokeagent.capabilities import Registry, check_args
from pokeagent.emulator.client import EmulatorError, RequestRefused
from pokeagent.memory import Memory
from pokeagent.server import NOTES_LIMIT, GameServer

buffer = io.BytesIO()
Image.new("RGB", (1, 1)).save(buffer, "PNG")
PNG = buffer.getvalue()


class FakeGame:
    def full_state(self):
        return "the state", PNG

    def act(self, name, **args):
        if args.get("button") == "turbo":
            raise ValueError("button must be one of a, b, ...")
        if args.get("button") == "crash":
            raise EmulatorError("emulator worker exited with -9")
        return f"{name} done", None

    def observations(self):
        return {"map_id": 1, "badges": 0, "in_game": True}

    def try_observation(self, spec):
        return 7

    def peek(self, address, length):
        if address < 0x8000:
            raise RequestRefused(f"address {address:#06x} is not readable RAM")
        return bytes(range(length))

    def run_tool(self, name, args):
        tool = self.registry.get(name, "tool")
        return f"run_tool {name}: returned 1", None, tool, None

    def run_macro(self, name, args):
        macro = self.registry.get(name, "macro")
        check_args(macro["spec"], args)
        error = "stuck" if args.get("stuck") else None
        return f"run_macro {name}: {'failed: stuck' if error else 'done'}", None, macro, error


@pytest.fixture
def server(tmp_path):
    memory = Memory(tmp_path / "memory.db", "run-1")
    memory.open_run({"objective": "catch a Pikachu", "rom_sha1": "red", "family": "pokemon-gen1"})
    game = FakeGame()
    game.registry = Registry(memory)
    s = GameServer(game, memory, tmp_path / "notes.md", tmp_path / "issues.md")
    url = s.start()
    yield s, url
    s.stop()


def call(url, token, tool, args=None):
    async def go():
        async with (
            httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http,
            streamable_http_client(url, http_client=http) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            return await session.call_tool(tool, args or {})

    return asyncio.run(go())


def text(result) -> str:
    return "".join(c.text for c in result.content if c.type == "text")


def status(url, headers) -> int:
    request = urllib.request.Request(url, data=b"{}", headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as e:
        return e.code


def test_requests_need_an_open_session_and_this_host(server):
    s, url = server
    assert status(url, {}) == 401
    assert status(url, {"Authorization": "Bearer guessed"}) == 401
    session = s.open_session(1, budget=5)
    assert status(url, {"Authorization": f"Bearer {session.token}", "Host": "evil.example"}) in (400, 421)
    assert not call(url, session.token, "get_state").is_error
    s.close_session(session)
    assert status(url, {"Authorization": f"Bearer {session.token}"}) == 401


def test_action_budget_and_end_session(server):
    s, url = server
    session = s.open_session(1, budget=2)
    assert text(call(url, session.token, "press", {"button": "a"})) == "[event 1] press done"
    refused = call(url, session.token, "press", {"button": "turbo"})
    assert refused.is_error and "button must be" in text(refused)
    over = call(url, session.token, "walk", {"direction": "up"})
    assert over.is_error and "budget" in text(over)
    state = call(url, session.token, "get_state")  # free
    assert not state.is_error and [c.type for c in state.content] == ["text", "image"]
    call(url, session.token, "end_session", {"summary": "left the house", "next_focus": "find Oak"})
    assert (session.summary, session.next_focus, session.ended) == ("left the house", "find Oak", True)
    assert "ended" in text(call(url, session.token, "advance_text"))


def test_notes_and_harness_issues(server, tmp_path):
    s, url = server
    token = s.open_session(3, budget=1).token
    assert text(call(url, token, "notes_read")) == "(empty)"
    call(url, token, "notes_write", {"text": "go north"})
    assert (tmp_path / "notes.md").read_text() == "go north"
    assert call(url, token, "notes_write", {"text": "x" * (NOTES_LIMIT + 1)}).is_error
    call(url, token, "report_harness_issue", {"description": "walk_to stops early", "evidence": "event 7"})
    assert "run run-1, session 3" in (tmp_path / "issues.md").read_text()


def test_a_failed_emulator_stops_every_game_tool(server):
    s, url = server
    token = s.open_session(1, budget=10).token
    crashed = call(url, token, "press", {"button": "crash"})
    assert crashed.is_error and "emulator has stopped" in text(crashed) and "exited with -9" in text(crashed)
    assert "emulator has stopped" in text(call(url, token, "get_state"))
    assert not call(url, token, "notes_read").is_error


def test_knowledge_and_objective_tools(server):
    s, url = server
    token = s.open_session(2, budget=5).token
    event = int(text(call(url, token, "press", {"button": "a"})).split("]")[0].removeprefix("[event "))
    added = {"kind": "mechanic", "topic": "Doors", "content": "walk into a door to enter", "evidence": event}
    assert text(call(url, token, "knowledge_add", added)) == "recorded as #1"
    assert "#1 [mechanic, confirmed, rom] Doors" in text(call(url, token, "knowledge_search", {"query": "door"}))
    assert call(url, token, "knowledge_add", added | {"kind": "rumour"}).is_error
    assert text(call(url, token, "knowledge_set_status", {"entry": 1, "status": "refuted"})) == "updated"
    assert text(call(url, token, "knowledge_search", {"query": "door"})) == "nothing found"

    badge = {"obs": "badges", "op": ">=", "value": 1}
    proposed = text(call(url, token, "propose_success_check", {"condition": badge, "reason": "the first gym"}))
    assert proposed == "recorded as check #1 (badges >= 1); it fails now"
    unknown = {"condition": {"obs": "hp", "op": "==", "value": 1}, "reason": ""}
    assert "unknown observation" in text(call(url, token, "propose_success_check", unknown))
    claimed = text(call(url, token, "report_objective_complete", {"evidence": "caught one"}))
    assert claimed.startswith("Recorded") and s.sessions[token].claim == "caught one"
    s.memory.add_check(badge, "user")
    assert text(call(url, token, "report_objective_complete", {"evidence": "x"})).startswith("Not recorded")


def test_a_failed_emulator_is_restarted_while_restarts_remain(server):
    s, url = server
    token = s.open_session(1, budget=10).token
    restarts = []
    s.recover = lambda error: restarts.append(error) or len(restarts) < 2
    crashed = text(call(url, token, "press", {"button": "crash"}))
    assert "was restarted" in crashed and "had no effect" in crashed and restarts == ["emulator worker exited with -9"]
    assert s.broken is None and not call(url, token, "press", {"button": "a"}).is_error
    assert s.memory.events("action")[-1]["data"]["args"] == {"button": "a", "times": 1}  # no event for the crash
    assert "end the session" in text(call(url, token, "press", {"button": "crash"}))
    assert s.broken and "emulator has stopped" in text(call(url, token, "get_state"))


def test_capability_tools(server):
    s, url = server
    session = s.open_session(1, budget=10)

    def use(tool, **args):
        return call(url, session.token, tool, args)

    hp = {"name": "hp", "description": "lead HP", "address": 0xD16C}
    assert "reads 7 now" in text(use("define_observation", **hp, expect={"op": "==", "value": 7}))
    refused = use("define_observation", **hp | {"name": "hp_high"}, expect={"op": ">", "value": 7})
    assert refused.is_error and "reads 7 now, which fails" in text(refused)
    assert use("define_observation", **hp | {"name": "map_id"}).is_error  # a builtin name
    assert text(use("mem_read", address=0xD16C, length=3)) == "0xd16c: 00 01 02"
    assert "not readable RAM" in text(use("mem_read", address=0x0100))  # a refused read, not a broken emulator
    assert s.broken is None

    steps = [{"do": "walk", "args": {"direction": "up"}, "until": {"obs": "hp", "op": "<", "value": 3}, "max": 5}]
    params = {"type": "object", "properties": {"stuck": {"type": "boolean"}}}
    defined = use("define_macro", name="climb", description="walk up", steps=steps, params=params)
    assert "climb version 1" in text(defined)
    assert use("define_macro", name="hp", description="x", steps=steps).is_error  # an observation's name
    assert use("define_macro", name="up", description="x", steps=[{"do": "fly"}]).is_error
    assert text(use("run_macro", name="climb")) == "[event 3] run_macro climb: done"  # after two definitions
    assert "do not match" in text(use("run_macro", name="climb", args={"stuck": "yes"}))  # refused, not counted
    assert "rolled back" not in text(use("run_macro", name="climb", args={"stuck": True}))
    assert "rolled back" not in text(use("run_macro", name="climb", args={"stuck": True}))
    third = text(use("run_macro", name="climb", args={"stuck": True}))
    assert "climb version 1 was rolled back (3 of its last 10 calls failed); no earlier version exists" in third
    assert "no active macro" in text(use("run_macro", name="climb"))
    assert text(use("list_capabilities")).startswith("hp (observation v1, rom; 1 byte(s) at 0xd16c as u8)")
    assert session.actions == 6  # every run_macro call is one action
    assert "no active tool" in text(use("run_tool", name="climb"))  # a macro is not a tool
    assert session.actions == 7
