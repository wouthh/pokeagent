---
title: "Treat game RAM as noise until a game is loaded, because the emulator randomises it at power-on"
author: Wout
date: 2026-11-27
branch: phase4-play-loop
id: l-2fbec180
scope: repo
status: active
src: agent
touches:
  - docs/architecture.md
  - src/pokeagent/games/pokemon_gen1.py
  - src/pokeagent/play.py
  - tests/test_gen1.py
---

## Decided
The Red/Blue state reader reports `in_game` (map size set and a decodable player name). Until then the brief and tool results show only the screen, walking is refused, and the trusted "game finished" check is not evaluated. Every field decoder must accept any byte values.

## Tried and ruled out
- Assuming power-on WRAM is zeros: SameBoy randomises it on every boot, so the second live run attempt crashed before its first session on money bytes that were not BCD (04484a). The first attempt had happened to get decodable noise, so the bug only shows on some boots.
- Evaluating the Hall of Fame check on any state: a random map id can equal 0x76 and would end a run at power-on.

## Watch out
- New observations or decoders must tolerate arbitrary bytes; the random-RAM test feeds 200 noise images through state(), state_text(), observations() and fingerprint().
- Anything built from RAM pointers (the walk grid) must not run before a game is loaded.

In the code: src/pokeagent/games/pokemon_gen1.py (state `in_game`, bcd, state_text), src/pokeagent/play.py (Game._do walk guard, Runner.loop check), tests/test_gen1.py (test_state_tolerates_any_ram, test_power_on_is_not_a_game_in_progress).
