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
from urllib.parse import unquote, urlsplit

MAX_PAYLOAD = 200
_TOKEN_RUNS = (re.compile(r"[0-9a-fA-F]{24,}"), re.compile(r"[A-Za-z0-9+/=]{32,}"))
_LOCAL_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home", ".arpa")


def _token_like(text: str) -> bool:
    # Check every percent-decoding level too: %-encoding each character would otherwise split any run.
    seen = text
    while (decoded := unquote(seen)) != seen:
        text, seen = text + "\n" + decoded, decoded
    for pattern in _TOKEN_RUNS:
        for match in pattern.finditer(text):
            run = match.group()
            if any(c.isdigit() for c in run) and any(c.isalpha() for c in run):
                return True
    return False


def _host_problem(host: str) -> str | None:
    host = host.rstrip(".")  # an absolute DNS name (localhost., router.lan.) is the same host
    if not host:
        return "no host"
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        # A numeric last label (127.1, 0x7f.0.0.1) is an alternate IPv4 spelling; TLDs are never numeric.
        numeric_tld = re.fullmatch(r"\d+|0x[0-9a-f]*", host.rsplit(".", 1)[-1], re.IGNORECASE)
        if host == "localhost" or "." not in host or numeric_tld or host.endswith(_LOCAL_SUFFIXES):
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
        host = parts.hostname or ""
        if "%" in host:
            # The fetcher percent-decodes hostnames (127%2e0.0%2e1 is 127.0.0.1); only a plain host is allowed.
            return "percent-encoded host"
        if problem := _host_problem(host):
            return problem
        if _token_like(host):
            return "host contains a long token-like string"
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
