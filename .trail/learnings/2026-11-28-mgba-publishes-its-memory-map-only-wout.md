---
title: "mGBA publishes its memory map only after the first frame, and SYSTEM_RAM is not IWRAM"
author: Wout
date: 2026-11-28
branch: gen3
id: l-42765f33
scope: repo
status: active
src: agent
touches:
  - AGENTS.md
  - README.md
  - docs/architecture.md
  - src/pokeagent/develop.py
  - src/pokeagent/emulator/cores.py
  - src/pokeagent/emulator/worker.py
  - src/pokeagent/extensions.py
  - src/pokeagent/games/__init__.py
  - src/pokeagent/play.py
  - src/pokeagent/prompts/play.md
---

## Decided
The worker takes its readable regions after the priming `session.run()`; `ram_hash` hashes the EWRAM and IWRAM descriptors whole on a GBA core.
`advance_text` presses B. A finished message still waits for A or B while the script engine's mode is 2 and its native wait is WaitForAorBPress; the printer is already inactive then.

## Tried and ruled out
Reading `session.memory_maps` before the first run (it is None, so every GBA read fails). `get_memory(2)` as the work RAM: on mGBA it is only the first 32 KiB of EWRAM, so IWRAM and most of EWRAM were left out of the hash.
Pressing A: it re-triggers the NPC in front of you once the box closes (endless dialogue loops). Reading the script's native pointer at any mode: it keeps a stale value after the script stopped.

## Watch out
EWRAM (256 KiB) is larger than one read (64 KiB): `Game.peek` chunks. The GB hash path is unchanged (no 0x02000000 region).
The Start menu's save question is no task of its own (its YES/NO lives in the menu task), so `save_game` answers it by the menu cursor while no text prints. Ruby and Sapphire have no text printers: use the message window state and `sMessageBoxMode`.
