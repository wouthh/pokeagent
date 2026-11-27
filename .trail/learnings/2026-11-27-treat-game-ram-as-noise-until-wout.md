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
Every press runs at least its wait, then keeps running frames (up to 32 in all) until the tile map changes. Results and the next tool call then see the game's reaction instead of the menu that was just answered.

## Tried and ruled out
- Assuming power-on WRAM is zeros: SameBoy randomises it on every boot, so the second live run attempt crashed before its first session on money bytes that were not BCD (04484a). The first attempt had happened to get decodable noise, so the bug only shows on some boots.
- Evaluating the Hall of Fame check on any state: a random map id can equal 0x76 and would end a run at power-on.
- Suspecting dropped inputs, as the agent's notes in live run 1a did ("first press in a fresh menu often ignored", "B on yes/no didn't work"): a press held for 1 to 6 frames at any frame offset was always accepted.
- The real cause: after A on HEAL or YES, the box and its ▶ cursor stay on screen for about 16 frames, and a press returned after 14. advance_text then saw a cursor, reported "menu" without pressing, and the agent pressed again.

## Watch out
- New observations or decoders must tolerate arbitrary bytes; the random-RAM test feeds 200 noise images through state(), state_text(), observations() and fingerprint().
- Anything built from RAM pointers (the walk grid) must not run before a game is loaded.

In the code: src/pokeagent/games/pokemon_gen1.py (state `in_game`, bcd, state_text), src/pokeagent/play.py (Game._do walk guard, Runner.loop check), tests/test_gen1.py (test_state_tolerates_any_ram, test_power_on_is_not_a_game_in_progress).
- A press that changes nothing (A facing a wall) now takes 32 frames instead of 14; that is cheap.
- The blinking ▼ arrow counts as a change, which is fine because the fixed wait has already passed by then.

In the code: src/pokeagent/games/pokemon_gen1.py (press, REACT_FRAMES), tests/test_gen1.py (test_press_waits_for_a_menu_to_react).
