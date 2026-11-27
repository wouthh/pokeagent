---
title: "The live agent asked for no development session in three play sessions, and a harness request showed the develop session works"
author: Wout
date: 2026-11-27
branch: phase7-develop
id: l-cb181831
scope: repo
status: active
src: agent
touches:
  - docs/architecture.md
  - src/pokeagent/prompts/develop.md
  - src/pokeagent/play.py
---

## Decided
Three 100-action play sessions on the phase 7a code (a scratch copy of run 1a, Pewter City to Misty) produced no request_development call: the agent kept building macros instead (7 more, 38 calls, 3 failed). That is the expected risk for A13, not a bug, so the prompts were not changed to push for it.
To check the develop path itself before long runs, one develop session was driven on a scratch copy with a harness request ("probe", naming no gameplay capability). It is plumbing evidence only, never A13 evidence.

## Tried and ruled out
Naming a capability or nudging the play prompt toward development: ruled out by Wout's rule against manufacturing an improvement to satisfy a test.

## Watch out
- In that develop session the agent's first test did `from tool import run`, which fails in the sandbox; tests must call the tool via ctx.call("<name>"). The develop prompt now says so.
- The CLI accepted the develop port's tool list under the same init check as play (the MCP server is named game on both ports).

In the code: src/pokeagent/prompts/develop.md, src/pokeagent/play.py (Runner._develop), docs/architecture.md (Develop mode, Live check).
