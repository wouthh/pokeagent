---
title: "Drive Gen 1 menus and text by reading the screen, never by fixed timings or blind A presses"
author: Wout
date: 2026-11-27
branch: phase3-gen1-adapter
id: l-00af822d
scope: repo
status: active
src: agent
touches:
  - AGENTS.md
  - README.md
  - docs/architecture.md
  - src/pokeagent/emulator/worker.py
  - tests/test_emulator.py
---

## Decided
Macro actions in the Red/Blue adapter read the tile map after every few frames and act on what is shown (cursor ▶, waiting arrow ▼, text box corners), instead of fixed press-and-wait scripts.

## Tried and ruled out
- Pressing A blindly through the intro: it lands in the naming screen and types letters. advance_text now stops at any cursor and leaves the choice to the caller.
- A fixed number of quiet frames between text boxes: Oak's fades are longer than the gaps, so text counts as closed only after 60 quiet frames, and callers loop.
- Reading the walk grid right after a warp: the map id changes about 30 frames before the blocks load; step waits for the map buffer to change and input to be accepted.
- Expecting walk_to to return "dialog" for map events: Oak's Pallet Town event starts a few frames after the step ends, so the walk reports "arrived".

## Watch out
- Red and Blue swap the preset names; pick the entry under NEW NAME by reading it, not by a literal.
- SameBoy will not serialize before its first frame; the worker runs one frame at startup.
- In this repo's code: src/pokeagent/games/pokemon_gen1.py (advance_text, choose, _settle_map), tests/test_gen1.py (boot fixture).
