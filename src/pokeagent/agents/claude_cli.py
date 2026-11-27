"""Agent provider that runs one `claude -p` session per call on the user's own Claude login.

Isolation (verified in the spikes, see docs/architecture.md):
- a scrubbed environment, so a parent agent's CLAUDE_*/ANTHROPIC_* variables never reach the session;
- only WebSearch, WebFetch and pokeagent's MCP server (`--tools`, `--strict-mcp-config`);
- no user or project settings, hooks, plugins, skills or slash commands (`--setting-sources ""`,
  `--disable-slash-commands`), and `--settings` that disables the CLI's builtin AGENTS.md plugin and auto-memory;
- the stream's `init` event is checked and the session killed if anything else shows up (fail closed).
`--bare` is never used: it ignores the claude.ai login.
"""

import contextlib
import json
import os
import shlex
import signal
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

ENV_KEEP = ("HOME", "PATH", "USER", "LOGNAME", "LANG")
WEB_TOOLS = frozenset({"WebFetch", "WebSearch"})
MCP_PREFIX = "mcp__game__"


class ProviderError(RuntimeError):
    """The provider cannot run sessions (not logged in, CLI missing); the run must stop."""


class InitRejected(ProviderError):
    """A session's init event showed tools, servers, plugins or settings outside the allowed set."""


@dataclass
class SessionSpec:
    brief: str
    system_prompt_file: Path
    mcp_url: str
    token: str
    cwd: Path  # the run directory
    stream_path: Path  # raw stream-json is saved here
    web_log: Path  # the web hook appends every request here
    max_turns: int
    timeout: float
    model: str | None = None
    effort: str | None = None


@dataclass
class SessionResult:
    end: str  # completed | max_turns | limit | auth_error | error | timeout
    num_turns: int = 0
    usage: dict = field(default_factory=dict)
    cost_usd: float = 0.0
    resets_at: int | None = None
    error: str | None = None


def scrubbed_env() -> dict[str, str]:
    return {k: os.environ[k] for k in ENV_KEEP if k in os.environ}


def preflight(claude: str = "claude") -> str:
    """Require a claude.ai login; return the CLI version line for play.log."""
    try:
        status = subprocess.run(
            [claude, "auth", "status", "--json"],
            env=scrubbed_env(),
            capture_output=True,
            text=True,
            timeout=60,
        )
        version = subprocess.run(
            [claude, "--version"], env=scrubbed_env(), capture_output=True, text=True, timeout=60
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ProviderError(f"cannot run the Claude CLI: {e}") from e
    try:
        auth = json.loads(status.stdout)
    except json.JSONDecodeError:
        auth = {}
    if not auth.get("loggedIn") or auth.get("authMethod") != "claude.ai":
        raise ProviderError("the Claude CLI is not logged in with a claude.ai account; run `claude auth login`")
    return version


def session_settings(web_log: Path) -> str:
    hook = shlex.join([sys.executable, "-m", "pokeagent", "hook", "web", "--log", str(web_log)])
    return json.dumps(
        {
            "enabledPlugins": {"cc-plugin-agents-md@builtin": False},
            "autoMemoryEnabled": False,
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "WebFetch|WebSearch",
                        "hooks": [{"type": "command", "command": hook}],
                    }
                ]
            },
        }
    )


def build_argv(spec: SessionSpec, mcp_config: Path, claude: str = "claude") -> list[str]:
    argv = [claude, "-p", spec.brief, "--output-format", "stream-json", "--verbose"]
    argv += ["--system-prompt-file", str(spec.system_prompt_file)]
    argv += ["--mcp-config", str(mcp_config), "--strict-mcp-config"]
    argv += ["--tools", "WebSearch,WebFetch", "--allowedTools", f"{MCP_PREFIX}*,WebSearch,WebFetch"]
    argv += [
        "--permission-mode",
        "dontAsk",
        "--setting-sources",
        "",
        "--settings",
        session_settings(spec.web_log),
    ]
    argv += [
        "--disable-slash-commands",
        "--no-session-persistence",
        "--max-turns",
        str(spec.max_turns),
    ]
    if spec.model:
        argv += ["--model", spec.model]
    if spec.effort:
        argv += ["--effort", spec.effort]
    return argv


def check_init(event: dict) -> None:
    """Fail closed on anything beyond the web tools, the game server and the CLI's own builtin plugins."""
    problems = []
    extra = [t for t in event.get("tools", []) if t not in WEB_TOOLS and not t.startswith(MCP_PREFIX)]
    if extra:
        problems.append(f"unexpected tools {extra}")
    servers = [(s.get("name"), s.get("status")) for s in event.get("mcp_servers", [])]
    if servers != [("game", "connected")]:
        problems.append(f"MCP servers {servers}")
    plugins = event.get("plugins", [])
    bad = [p.get("name") for p in plugins if p.get("path") != "builtin" or p.get("name") == "cc-plugin-agents-md"]
    if bad:
        problems.append(f"plugins {bad}")
    for key in ("slash_commands", "skills", "memory_paths"):
        if event.get(key):
            problems.append(f"{key} {event[key]}")
    if event.get("permissionMode") != "dontAsk":
        problems.append(f"permission mode {event.get('permissionMode')}")
    if problems:
        raise InitRejected("Claude session init rejected: " + "; ".join(problems))


def _write_mcp_config(spec: SessionSpec) -> Path:
    path = spec.stream_path.with_suffix(".mcp.json")
    config = {
        "mcpServers": {
            "game": {
                "type": "http",
                "url": spec.mcp_url,
                "headers": {"Authorization": f"Bearer {spec.token}"},
            }
        }
    }
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(config, f)
    return path


def _limit_resets_at(event: dict) -> int | None:
    info = event.get("rate_limit_info") or {}
    status = str(info.get("status", "allowed"))
    return None if status.startswith("allowed") else int(info.get("resetsAt") or 0)


def run_session(
    spec: SessionSpec,
    on_event: Callable[[str, dict], None] = lambda kind, data: None,
    claude: str = "claude",
) -> SessionResult:
    """Run one session to its end. Raises InitRejected (the run must stop); everything else is a SessionResult.

    Neutral events passed to `on_event`: text, tool_call, tool_result, limit, end.
    """
    mcp_config = _write_mcp_config(spec)
    timed_out = threading.Event()
    result = SessionResult(end="error", error="session ended without a result")
    rejected: InitRejected | None = None
    validated = False
    try:
        proc = subprocess.Popen(
            build_argv(spec, mcp_config, claude),
            cwd=spec.cwd,
            env=scrubbed_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        def stop() -> None:
            timed_out.set()
            _kill(proc)

        watchdog = threading.Timer(spec.timeout, stop)
        watchdog.start()
        try:
            with spec.stream_path.open("ab") as stream:
                for raw in proc.stdout:
                    stream.write(raw)
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    kind = event.get("type")
                    if kind == "system" and event.get("subtype") == "init":
                        try:
                            check_init(event)
                        except InitRejected as e:
                            rejected = e
                        except (AttributeError, TypeError) as e:
                            rejected = InitRejected(f"Claude session init malformed: {e!r}")
                        if rejected:
                            _kill(proc)
                            break
                        validated = True
                    elif kind in ("assistant", "user", "result") and not validated:
                        rejected = InitRejected("Claude session produced output before a validated init event")
                        _kill(proc)
                        break
                    elif kind == "assistant":
                        for block in event.get("message", {}).get("content", []):
                            if block.get("type") == "text":
                                on_event("text", {"text": block["text"]})
                            elif block.get("type") == "tool_use":
                                on_event(
                                    "tool_call",
                                    {"name": block["name"], "input": block.get("input")},
                                )
                    elif kind == "user":
                        for block in event.get("message", {}).get("content", []):
                            if isinstance(block, dict) and block.get("type") == "tool_result":
                                on_event("tool_result", {"error": bool(block.get("is_error"))})
                    elif kind == "rate_limit_event" and (resets := _limit_resets_at(event)) is not None:
                        result.resets_at = resets
                        on_event("limit", {"resets_at": resets})
                    elif kind == "result":
                        result = _result(event, result.resets_at)
            proc.wait()
            if not validated and not rejected and not timed_out.is_set():
                rejected = InitRejected("Claude session ended without a validated init event")
        except BaseException:
            _kill(proc)
            proc.wait()
            raise
        finally:
            watchdog.cancel()
    finally:
        mcp_config.unlink(missing_ok=True)
    if rejected:
        raise rejected
    if timed_out.is_set():
        result.end, result.error = "timeout", f"session exceeded {spec.timeout:g}s"
    on_event("end", {"end": result.end, "num_turns": result.num_turns, "cost_usd": result.cost_usd})
    return result


def _result(event: dict, resets_at: int | None) -> SessionResult:
    result = SessionResult(
        end="error",
        num_turns=event.get("num_turns") or 0,
        usage=event.get("usage") or {},
        cost_usd=event.get("total_cost_usd") or 0.0,
        resets_at=resets_at,
    )
    subtype, status = event.get("subtype"), event.get("api_error_status")
    text = str(event.get("result") or event.get("errors") or "")
    if subtype == "success" and not event.get("is_error"):
        result.end = "completed"
    elif subtype == "error_max_turns":
        result.end = "max_turns"
    elif status == 429 or resets_at is not None or "limit" in text.lower():
        result.end = "limit"
    elif status in (401, 403) or any(w in text.lower() for w in ("authenticat", "log in", "login", "oauth")):
        result.end = "auth_error"
    if result.end != "completed":
        result.error = text[:500] or f"{subtype} {status}"
    return result


def _kill(proc: subprocess.Popen) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)
