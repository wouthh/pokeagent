---
title: "Claude sessions are judged at their init event, and usage-limit detection is still unverified"
author: Wout
date: 2026-11-27
branch: phase4a-claude-provider
id: l-95697779
scope: repo
status: active
src: agent
touches:
  - src/pokeagent/agents/claude_cli.py
  - src/pokeagent/webhook.py
  - tests/test_claude_cli.py
---

## Decided
- A session is checked at its stream-json `init` event and killed there on any unexpected tool, MCP server, non-builtin plugin, skill, slash command or memory path. `init` arrives before the first model request, so a rejection costs no tokens.
- Usage limits are classified from structured fields first (`rate_limit_event.rate_limit_info.status` not starting with "allowed", or `result.api_error_status` 429), with a text match on "limit" as the fallback.
- The review loop for this repo needs a worktree with the PR branch checked out; start `prloop run` from the main checkout with `--worktree <that worktree>`.

## Tried and ruled out
- Starting `prloop run` without `--worktree` while the main checkout is on `main`: it stops with "no worktree has <branch> checked out" (exit 50).

## Watch out
- The limit path has only been tested with a fake stream. The first real limit stream must become a test fixture, and the classification must be adjusted to it.
- The web hook is a separate process started by the CLI (`python -m pokeagent hook web`); a crash in it surfaces to the model as a hook error, not as a block decision.

In the code: src/pokeagent/agents/claude_cli.py (check_init, _limit_resets_at, _result), src/pokeagent/webhook.py; tests/test_claude_cli.py uses a generated fake `claude` executable.
