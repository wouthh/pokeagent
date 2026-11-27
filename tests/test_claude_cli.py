import json
import sys
import time
from pathlib import Path

import pytest

from pokeagent.agents import claude_cli as cc

INIT = {
    "type": "system",
    "subtype": "init",
    "tools": ["WebFetch", "WebSearch", "mcp__game__get_state"],
    "mcp_servers": [{"name": "game", "status": "connected", "source": "dynamic"}],
    "plugins": [{"name": "cc-plugin-telemetry", "path": "builtin", "source": "cc-plugin-telemetry@builtin"}],
    "slash_commands": [],
    "skills": [],
    "memory_paths": None,
    "permissionMode": "dontAsk",
}
RESULT = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "num_turns": 3,
    "total_cost_usd": 0.01,
    "usage": {
        "input_tokens": 10,
        "cache_creation_input_tokens": 100,
        "cache_read_input_tokens": 5,
        "output_tokens": 20,
    },
}


def fake_claude(tmp_path: Path, events: list[dict], *, sleep: float = 0, auth: dict | None = None) -> str:
    """A stand-in `claude` that records its argv, environment and MCP config mode, then prints `events`."""
    script = tmp_path / "claude"
    script.write_text(f"""#!{sys.executable}
import json, os, stat, sys, time
argv = sys.argv[1:]
if argv[:2] == ["auth", "status"]:
    print(json.dumps({auth or {}!r})); sys.exit(0)
if argv == ["--version"]:
    print("9.9.9 (Claude Code)"); sys.exit(0)
mcp = argv[argv.index("--mcp-config") + 1]
seen = {{"argv": argv, "env": sorted(os.environ), "mcp_mode": oct(stat.S_IMODE(os.stat(mcp).st_mode)),
        "mcp": json.load(open(mcp))}}
open({str(tmp_path / "seen.json")!r}, "w").write(json.dumps(seen))
for event in {events!r}:
    print(json.dumps(event), flush=True)
time.sleep({sleep})
""")
    script.chmod(0o755)
    return str(script)


def spec(tmp_path: Path, **kw) -> cc.SessionSpec:
    (tmp_path / "play.md").write_text("system prompt")
    defaults = dict(
        brief="kickoff",
        system_prompt_file=tmp_path / "play.md",
        mcp_url="http://127.0.0.1:1/mcp",
        token="t0ken",
        cwd=tmp_path,
        stream_path=tmp_path / "0001.jsonl",
        web_log=tmp_path / "web.jsonl",
        max_turns=120,
        timeout=20,
    )
    return cc.SessionSpec(**{**defaults, **kw})


def test_argv_isolation_flags(tmp_path):
    argv = cc.build_argv(spec(tmp_path, model="sonnet", effort="high"), tmp_path / "x.mcp.json")
    assert "--bare" not in argv
    for flag, value in [
        ("--tools", "WebSearch,WebFetch"),
        ("--setting-sources", ""),
        ("--permission-mode", "dontAsk"),
        ("--max-turns", "120"),
        ("--model", "sonnet"),
        ("--effort", "high"),
    ]:
        assert argv[argv.index(flag) + 1] == value
    for flag in ("--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"):
        assert flag in argv
    settings = json.loads(argv[argv.index("--settings") + 1])
    assert settings["enabledPlugins"] == {"cc-plugin-agents-md@builtin": False}
    assert settings["autoMemoryEnabled"] is False
    hook = settings["hooks"]["PreToolUse"][0]
    assert hook["matcher"] == "WebFetch|WebSearch"
    assert "hook web --log" in hook["hooks"][0]["command"]


def test_init_check_accepts_expected_shape():
    cc.check_init(INIT)


@pytest.mark.parametrize(
    "change",
    [
        {"tools": [*INIT["tools"], "Bash"]},
        {"mcp_servers": [*INIT["mcp_servers"], {"name": "other", "status": "connected"}]},
        {"mcp_servers": [{"name": "game", "status": "failed"}]},
        {"plugins": [{"name": "ponytail", "path": "/home/x/.claude/plugins/ponytail"}]},
        {"plugins": [{"name": "cc-plugin-agents-md", "path": "builtin"}]},
        {"skills": ["trail"]},
        {"slash_commands": ["loop"]},
        {"memory_paths": {"auto": "/home/x/.claude/projects/y/memory/"}},
        {"permissionMode": "bypassPermissions"},
    ],
)
def test_init_check_fails_closed(change):
    with pytest.raises(cc.InitRejected):
        cc.check_init({**INIT, **change})


def test_session_success(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://proxy")
    events = [
        INIT,
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "hi"},
                    {"type": "tool_use", "name": "mcp__game__get_state", "input": {}},
                ]
            },
        },
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
        RESULT,
    ]
    seen_events = []
    s = spec(tmp_path)
    result = cc.run_session(s, lambda k, d: seen_events.append(k), claude=fake_claude(tmp_path, events))
    assert result.end == "completed" and result.num_turns == 3 and result.usage["output_tokens"] == 20
    assert seen_events == ["text", "tool_call", "tool_result", "end"]
    seen = json.loads((tmp_path / "seen.json").read_text())
    assert not [k for k in seen["env"] if k.startswith(("CLAUDE", "ANTHROPIC"))]
    assert seen["mcp_mode"] == "0o600"
    assert seen["mcp"]["mcpServers"]["game"]["headers"]["Authorization"] == "Bearer t0ken"
    assert not list(tmp_path.glob("*.mcp.json"))  # the token file is removed after the session
    assert len(s.stream_path.read_text().splitlines()) == len(events)


def test_init_rejection_kills_session(tmp_path):
    bad = {**INIT, "tools": [*INIT["tools"], "Bash"]}
    start = time.monotonic()
    with pytest.raises(cc.InitRejected, match="Bash"):
        cc.run_session(spec(tmp_path), claude=fake_claude(tmp_path, [bad, RESULT], sleep=30))
    assert time.monotonic() - start < 10
    assert not list(tmp_path.glob("*.mcp.json"))


def test_max_turns_is_a_normal_end(tmp_path):
    end = {
        **RESULT,
        "subtype": "error_max_turns",
        "is_error": True,
        "errors": ["Reached maximum number of turns (5)"],
    }
    result = cc.run_session(spec(tmp_path), claude=fake_claude(tmp_path, [INIT, end]))
    assert result.end == "max_turns"


def test_usage_limit_is_detected(tmp_path):
    limit = {
        "type": "rate_limit_event",
        "rate_limit_info": {
            "status": "rejected",
            "resetsAt": 1795780200,
            "rateLimitType": "five_hour",
        },
    }
    end = {**RESULT, "subtype": "error_during_execution", "is_error": True, "api_error_status": 429}
    kinds = []
    result = cc.run_session(
        spec(tmp_path),
        lambda k, d: kinds.append(k),
        claude=fake_claude(tmp_path, [INIT, limit, end]),
    )
    assert result.end == "limit" and result.resets_at == 1795780200
    assert "limit" in kinds


def test_auth_error_is_detected(tmp_path):
    end = {**RESULT, "subtype": "error_during_execution", "is_error": True, "api_error_status": 401}
    assert cc.run_session(spec(tmp_path), claude=fake_claude(tmp_path, [INIT, end])).end == "auth_error"


def test_timeout_kills_session(tmp_path):
    start = time.monotonic()
    result = cc.run_session(spec(tmp_path, timeout=1), claude=fake_claude(tmp_path, [INIT], sleep=30))
    assert result.end == "timeout"
    assert time.monotonic() - start < 10


def test_preflight(tmp_path):
    good = fake_claude(tmp_path, [], auth={"loggedIn": True, "authMethod": "claude.ai"})
    assert cc.preflight(good) == "9.9.9 (Claude Code)"
    other = tmp_path / "other"
    other.mkdir()
    bad = fake_claude(other, [], auth={"loggedIn": True, "authMethod": "apiKey"})
    with pytest.raises(cc.ProviderError, match=r"claude\.ai"):
        cc.preflight(bad)
    with pytest.raises(cc.ProviderError, match="cannot run"):
        cc.preflight(str(tmp_path / "missing"))
