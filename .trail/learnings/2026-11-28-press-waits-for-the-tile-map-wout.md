---
title: "press waits for the tile map to settle: Gold's START menu is silent for 28 frames"
author: Wout
date: 2026-11-28
branch: gen12-press-settle
id: l-f673e923
scope: repo
status: active
src: agent
touches:
  - src/pokeagent/games/pokemon_gen1.py
  - tests/test_gen1.py
  - tests/test_gen2.py
---

## Decided
Gen1.press, after the screen first reacts, runs until the tile map has been still for SETTLE_STILL=32 frames (cap SETTLE_CAP=120). Measured live: Gold's START press changes the tile map at +4 frames, then nothing for 28 frames, then the menu is drawn by +40 and only then takes input (Red: +1 and +17). A smaller window (8) returned inside that gap and the next press was lost.
## Tried and ruled out
A RAM flag for "menu ready": bytes like wMenuSelection flip at about +40 but are game-specific; the tile map signal works for Gen 1 and 2 alike.
## Watch out
Printing text and blinking name-entry cursors never settle, so they run into the cap (results can show text mid-print). Gen 2's naming grid is not in a box, so screen_text is empty there although the tile map is settled.
