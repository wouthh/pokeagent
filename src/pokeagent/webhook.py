"""PreToolUse hook for the Claude sessions' WebFetch and WebSearch (`pokeagent hook web --log PATH`).

Logs every request and blocks the ones that could carry data out or reach local services: anything but a plain
http(s) URL with an ASCII host name, local or private hosts, long token-like runs (hex or base64), and path/query
or search text over 200 characters.
Exit code 2 blocks the call; the reason on stderr is shown to the model. Malformed input is blocked (fail closed).
"""

import ipaddress
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import unquote

MAX_PAYLOAD = 200
# A slash ends a base64 run like - and _ do: otherwise a path of words (/repos/pret/pokered/contents/maps/Route9)
# is one long run. Longer payloads are capped at MAX_PAYLOAD anyway.
_TOKEN_RUNS = (re.compile(r"[0-9a-fA-F]{24,}"), re.compile(r"[A-Za-z0-9+=]{32,}"))
_LOCAL_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home", ".arpa")
# Only a plain host name or IPv4 address and an optional port, then a printable-ASCII path. The fetcher's URL parser
# differs from Python's (backslashes, percent-encoding, Unicode dots, userinfo, IPv6 literals), so any authority
# outside this shape is blocked instead of being interpreted.
_URL = re.compile(r"(https?)://([a-z0-9.-]+)(?::\d{1,5})?([/?#][\x21-\x7e]*)?", re.IGNORECASE)


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
        ip = ipaddress.ip_address(host)
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
        if not (match := _URL.fullmatch(url)):
            return "only plain http(s) URLs with an ASCII host name are allowed"
        host = match.group(2).lower()
        if problem := _host_problem(host):
            return problem
        if _token_like(host):
            return "host contains a long token-like string"
        payload = match.group(3) or ""
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
