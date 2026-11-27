# Architecture

This document describes the target design, what is implemented so far, and the decisions behind it. Sections marked
*(planned)* are not built yet.

## Goal

pokeagent is an environment in which an agent plays emulated games autonomously towards a natural-language
objective, accumulates knowledge, and improves its own tooling when that helps. The model stays the same; its
harness, knowledge and capabilities evolve. Pokémon Red/Blue is the first use case and the first long-term
benchmark (a full playthrough without manual help).

## Components

```
pokeagent play/resume ── orchestrator (trusted, host)
  ├─ agent provider: ClaudeCliProvider → `claude -p` sessions (mode = play | develop)
  ├─ MCP server 127.0.0.1 (per-session token bound to mode; game-touching calls serialised)
  ├─ capability registry (builtin + declarative + code extensions, versioned, with contracts)
  ├─ memory store (SQLite + FTS5) + run files, timeline-aware
  ├─ stuck detector, policy, budgets, checkpoints, metrics
  ├─ viewer (Tk: frame + status panel)
  └─ child processes, each in its own bwrap sandbox with minimal mounts:
       live emulator worker   python/venv, package, core .so, ROM (ro); tmpfs
       extension host         python/venv, package, that capability version (ro); tmpfs; no ROM
       validation/dev worker  python/venv, package, core, ROM, staged copy, fixtures (ro); tmpfs
```

Thin interfaces exist only at these seams, each with one implementation for now:

| Interface | v1 implementation |
|---|---|
| `EmulatorBackend` (load, step with buttons, frame, read RAM, save/load state) | libretro.py + SameBoy core |
| `GameAdapter` (identify ROM, observations, state text, walk grid, progress score, predicates) | `pokemon_gen1` |
| `AgentProvider` (`run(SessionSpec) -> SessionResult`, neutral events) | `ClaudeCliProvider` |

## Data: local versus remote

- **Local only** (`~/.local/share/pokeagent/`, override `--data-dir`): ROM path, save states, the SQLite database,
  session streams, logs, the agent-built capability library, harness issues. Cores: `~/.cache/pokeagent/cores/`.
- **Remote (Anthropic, through the Claude CLI on the user's account):** everything the model sees: prompts, briefs,
  tool results, screenshots, RAM-derived text, notes, knowledge hits.
- **Remote (third parties):** WebSearch/WebFetch requests made by the CLI from the host.
- Nothing is published automatically.

## Isolation and permissions *(planned, phases 2–7)*

- Claude sessions run with only pokeagent's MCP server plus WebSearch/WebFetch (`--tools`, `--strict-mcp-config`,
  `--setting-sources ""`, `--permission-mode dontAsk`, `--disable-slash-commands`). Every session's `init` event is
  checked; unexpected tools, plugins or hooks stop the run.
- The MCP server binds to 127.0.0.1, checks host/origin, and maps each per-session bearer token to a mode. Dev
  sessions have no route to the live emulator.
- Agent-written code runs only in bubblewrap: `--unshare-all --die-with-parent --new-session --clearenv`, minimal
  read-only mounts, a tmpfs, rlimits and a wall-clock timeout. A startup probe (network connect and a read of `~`
  must fail) fails closed.
- No memory-write path exists. RAM reads are limited to RAM regions (never ROM banks).
- A PreToolUse hook on WebFetch/WebSearch logs every request and blocks payload-like URLs and private hosts.

## Objectives, memory, timelines *(planned)*

- The objective text and authoritative success checks (from the user, or the adapter for the default objective) are
  immutable. Agent-proposed checks are recorded but only pause the run for confirmation.
- Memory separates goals, milestones, knowledge (world, technical, strategy, lesson, limitation, progress; hypothesis
  versus confirmed; with provenance and scope by run, ROM sha1, game family or general), capabilities, sessions,
  events and inputs.
- Restoring a checkpoint opens a new timeline: timeline-bound state (goals, milestones, notes, progress knowledge,
  timeline KV) reverts; durable learning (knowledge, capabilities, durable KV) stays. Crash recovery replays only the
  current timeline's inputs, verified by RAM hashes.

## Capabilities *(planned)*

- **Builtin:** `get_state`, `press`, `walk_to(x, y)` (whole-map grid), `walk`, `advance_text` (stops at prompts).
- **Declarative:** RAM observations and macros, definable during play and usable in the same session.
- **Code extensions:** written in develop mode against a small `ctx` API, executed in the sandbox, activated at
  session boundaries.
- Validation is owned by the orchestrator: schemas, contracts (preconditions, postconditions, invariants, declared
  effects), a shared execution budget across nested calls, and runs on several game states. Contract violations,
  exceptions and timeouts count as runtime failures and roll a capability back automatically.

## Verified facts

Filled in by the implementation spikes; each entry says how it was verified.

## Decisions

- **2026-11-27: Unchanged upstream libretro cores through libretro.py, no emulator fork.** One Python API across
  GB/GBC (SameBoy), GBA (mGBA) and DS (melonDS DS); best-in-class accuracy without maintaining C code. Fork a single
  core only if it lacks a needed feature.
- **2026-11-27: The local Claude CLI (`claude -p`) is the first agent provider,** so runs use the user's own
  login without a Python SDK dependency. Other providers plug in behind `AgentProvider`.
- **2026-11-27: Python 3.13 via uv.** uv's managed CPython includes Tk (for the viewer) and SQLite FTS5.
- **2026-11-27: Tools over MCP streamable HTTP on 127.0.0.1,** served by the long-lived `pokeagent play` process, so
  the emulator, viewer and learned data survive between Claude sessions.
