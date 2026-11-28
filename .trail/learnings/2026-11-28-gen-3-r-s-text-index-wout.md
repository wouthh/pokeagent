---
title: "Gen 3 R/S text index, door-only exits and battle waits (pokemon_gen3.py)"
author: Wout
date: 2026-11-28
branch: gen3-text-doors
id: l-a06db933
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
Ruby/Sapphire text pointer is start plus textIndex; only warp events on 0x60-0x7F behaviour tiles are exits (others are bookkeeping: standing on them does nothing); a push into the wall from an exit tile waits for the warp; advance_text waits bounded through battle animations.

## Watch out
Warp events listed next to doors (FireRed's house has three) look like exits but are plain tiles.
