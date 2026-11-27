---
title: "The web hook allows only plain URLs instead of mirroring the fetcher's URL parser"
author: Wout
date: 2026-11-27
branch: phase4a-claude-provider
cost: { minutes: 3, tokens: 42289 }
id: l-acde990d
scope: repo
status: active
src: agent
touches:
  - src/pokeagent/webhook.py
  - docs/architecture.md
  - tests/test_webhook.py
  - src/pokeagent/agents/claude_cli.py
  - tests/test_claude_cli.py
  - AGENTS.md
  - src/pokeagent/agents/__init__.py
  - src/pokeagent/cli.py
---

## Decided
The WebFetch hook accepts a URL only when it has the plain shape: http or https, a host made of ASCII letters, digits, dots and hyphens, an optional port, then a printable-ASCII path. Every other authority is blocked, not interpreted. Locality checks (localhost, dotless names, numeric last labels, private IPs, local suffixes) run on that plain host.

## Tried and ruled out
Parsing with Python's urlsplit and blocking each known difference from the fetcher's parser, one review round at a time: percent-encoded hosts, Unicode dots, trailing DNS dots, numeric IPv4 spellings, then backslashes (`http://127.0.0.1\example.com/` reaches loopback because the fetcher treats `\` as `/`). Each fix invited the next bypass, so the parser-mirroring approach was dropped for an allowlist of the URL's shape.

## Watch out
- Don't loosen the shape to admit userinfo, IPv6 literals, percent-encoding or non-ASCII hosts without checking how the CLI's fetcher parses them; a blocked legitimate page costs little, a bypass reaches local services.
- DNS names that resolve to private addresses are not caught; the hook never resolves names.

In the code: src/pokeagent/webhook.py (`_URL`, `_host_problem`, `decide`), tests/test_webhook.py (blocked URL cases).
