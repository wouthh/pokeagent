---
title: "Provider and web hook fail closed on missing init, loop errors and log failures"
author: Wout
date: 2026-11-27
branch: phase4a-claude-provider
cost: { minutes: 1, tokens: 49844 }
id: l-befbd8e9
scope: repo
status: active
src: agent
touches:
  - src/pokeagent/webhook.py
  - src/pokeagent/agents/claude_cli.py
---

## Decided
A session needs a validated init before any assistant/user/result event; a missing init is rejected, and any exception kills and reaps the process group. The web hook blocks when its log is unwritable.

## Watch out
In the code: run_session in agents/claude_cli.py and main in webhook.py; exit 1 from a hook is non-blocking.
