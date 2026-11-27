"""PreToolUse hook for the Claude sessions' WebFetch and WebSearch (`pokeagent hook web --log PATH`).

Logs every request and blocks the ones that could carry data out or reach local services: non-http(s) URLs,
local or private hosts, long token-like runs (hex or base64), and path/query or search text over 200 characters.
Exit code 2 blocks the call; the reason on stderr is shown to the model. Malformed input is blocked (fail closed).
"""

import ipaddress
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

MAX_PAYLOAD = 200
_TOKEN_RUNS = (re.compile(r"[0-9a-fA-F]{24,}"), re.compile(r"[A-Za-z0-9+/=]{32,}"))
_LOCAL_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home", ".arpa")


def _token_like(text: str) -> bool:
    for pattern in _TOKEN_RUNS:
        for match in pattern.finditer(text):
            run = match.group()
            if any(c.isdigit() for c in run) and any(c.isalpha() for c in run):
                return True
    return False


def _host_problem(host: str) -> str | None:
    if not host:
        return "no host"
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        if host == "localhost" or "." not in host or host.endswith(_LOCAL_SUFFIXES):
            return f"local host {host}"
        return None
    return None if ip.is_global else f"non-public address {host}"


def decide(tool: str, tool_input: dict) -> str | None:
    """Return the reason to block, or None to allow."""
    if tool == "WebFetch":
        url = tool_input.get("url")
        if not isinstance(url, str):
            return "missing url"
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            return f"scheme {parts.scheme!r} is not allowed"
        if parts.username or parts.password:
            return "credentials in the URL"
        if problem := _host_problem(parts.hostname or ""):
            return problem
        payload = parts.path + ("?" + parts.query if parts.query else "")
    elif tool == "WebSearch":
        payload = tool_input.get("query")
        if not isinstance(payload, str):
            return "missing query"
    else:
        return f"unexpected tool {tool}"
    if len(payload) > MAX_PAYLOAD:
        return f"request text longer than {MAX_PAYLOAD} characters"
    if _token_like(payload):
        return "request contains a long token-like string"
    return None


def main(log: Path) -> int:
    try:
        event = json.load(sys.stdin)
        tool, tool_input = event["tool_name"], event["tool_input"]
        reason = decide(tool, tool_input)
    except (ValueError, KeyError, TypeError, AttributeError):
        tool, tool_input, reason = None, None, "malformed hook input"
    try:
        with log.open("a") as f:
            f.write(json.dumps({"time": time.time(), "tool": tool, "input": tool_input, "blocked": reason}) + "\n")
    except (OSError, TypeError, ValueError):
        reason = reason or "web log unavailable"
    if reason:
        print(f"Blocked by pokeagent's web policy: {reason}.", file=sys.stderr)
        return 2
    return 0
