import asyncio
import io
import urllib.error
import urllib.request

import httpx2
import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from PIL import Image

from pokeagent.emulator.client import EmulatorError
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


@pytest.fixture
def server(tmp_path):
    s = GameServer(FakeGame(), tmp_path / "notes.md", tmp_path / "issues.md", "run-1")
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
    assert text(call(url, session.token, "press", {"button": "a"})) == "press done"
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
