---
title: "Sandbox rlimits are set inside bwrap, and claude -p needs builtin plugins disabled"
author: Wout
date: 2026-11-27
branch: phase2-emulator
id: l-d3085f55
scope: repo
status: active
src: agent
touches:
  - ".github/workflows/ci.yml"
  - AGENTS.md
  - README.md
  - docs/architecture.md
  - pyproject.toml
  - scripts/check.sh
  - src/pokeagent/cli.py
  - tests/test_smoke.py
---

## Decided
- Rlimits for sandboxed processes are applied by the trusted bootstrap inside the bwrap namespace, never before bwrap.
- Claude sessions get a scrubbed environment and `--settings` that disables the CLI's builtin `cc-plugin-agents-md` plugin and auto-memory; `--bare` stays unused because it drops the claude.ai login.

## Tried and ruled out
- RLIMIT_NPROC set in the parent (preexec) counts every process of the user, so bwrap fails with "Creating new namespace failed: Resource temporarily unavailable".
- Running two libretro Sessions in one process: segfault (exit 139). One core per worker process.

## Watch out
- The SameBoy core prints to fd 1 itself even with `log=None`; the worker keeps RPC on a dup of the original stdout and points fd 1 at stderr.
- bwrap `--die-with-parent` follows the spawning thread: spawn sandboxes from a long-lived thread.
- A probe canary in /tmp proves nothing, because the sandbox mounts its own tmpfs there.

In the code: src/pokeagent/sandbox.py (_BOOT, ensure), src/pokeagent/emulator/worker.py (serve). Full spike facts: docs/architecture.md, Verified facts.
