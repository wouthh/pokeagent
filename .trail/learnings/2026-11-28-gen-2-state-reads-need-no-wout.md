---
title: "Gen 2 state reads need no SVBK guard: D000 is always bank 1"
author: Wout
date: 2026-11-28
branch: gen2-wram-banks
id: l-d4dbd592
scope: repo
status: active
src: agent
touches:
  - docs/architecture.md
  - src/pokeagent/games/pokemon_gen2.py
  - tests/test_gen2.py
---

## Decided
Remove the Gen 2 WRAM bank guard (this overrides the learning that state is read only while SVBK selects bank 1). SameBoy's memory map points D000 at bank 1 for every SVBK, checked over 4000 frames in Crystal (D000 equalled bank 1 in all 211 samples with SVBK 5 or 6 and differing banks). The guard turned a running game into "no game" for the frames when the game switches banks 5/6 in (START menu, battle start).
## Watch out
SYSTEM_RAM has all 8 banks in order (bank n at n*0x1000) if another bank is ever needed, but banks 2-7 are already at 0x10000+.
