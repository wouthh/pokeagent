---
title: Gen 3 in_game needs a loaded map; map names come from pret identifiers (pokemon_gen3.py)
author: Wout
date: 2026-11-28
branch: gen3-intro-fix
id: l-7ba6886b
scope: repo
status: active
src: agent
touches:
  - docs/architecture.md
  - src/pokeagent/games/pokemon_gen3.py
  - tests/test_gen3.py
---

## Decided
`in_game` also requires the map header (ROM pointers) and grid size to be valid: the intro and naming screen have a name but no map. Map names are pret identifiers by group and number in gen3_map_names_*.txt, with a section plus group.number fallback when they do not fit the header's section.

## Watch out
Header pointers must be checked as ROM offsets before indexing self.rom (grid raised IndexError during the intro).
