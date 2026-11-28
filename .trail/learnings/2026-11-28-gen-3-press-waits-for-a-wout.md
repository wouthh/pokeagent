---
title: Gen 3 press waits for a settled screen; R/S intro text is in the menu window (pokemon_gen3.py)
author: Wout
date: 2026-11-28
branch: gen3-input
id: l-328c0ae1
scope: repo
status: active
src: agent
touches:
  - docs/architecture.md
  - src/pokeagent/games/pokemon_gen3.py
  - src/pokeagent/play.py
  - tests/test_gen3.py
---

## Decided
`press` waits until the picture stands still before pressing and again after, because the game ignores input during fades and menu openings; `advance_text` uses `tap` (the blinking arrow never settles). R/S intro text is read from gMenuWindow as well as the field window.

## Watch out
A running script must not hide a full-screen menu (the clock): check the main callback before script mode.
