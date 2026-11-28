---
title: "Fix the agent's misreadings of a game with generic runner patterns, not per-game rules"
author: Wout
date: 2026-11-28
branch: game-state-patterns
id: l-ad6cd1ab
scope: repo
status: active
src: agent
touches:
  - docs/architecture.md
  - src/pokeagent/memory.py
  - src/pokeagent/play.py
  - tests/test_memory.py
  - tests/test_play.py
---

## Decided
Wout asked that fixes for an agent misunderstanding its game be patterns that work for any game: state read from memory, bag and party tracked, goals set from the real state. A Gold session spent 60 actions in Elm's lab believing it was Crystal and planning around items its notes assumed; the fix was generic runner code, not a Gen 2 rule.
- The brief names the game (run.json `game`) and puts the state read from RAM before the notes, saying the game outranks notes, plan and knowledge.
- Items obtained for the first time on a timeline are milestones (bag items of the loaded game count as already had, also after a title screen).
- Action results name items gained/lost and Pokémon that joined/left when party or bag changed.
- Knowledge recorded while playing another ROM says "learned in <title>" (or "another game" for a hack).

## Watch out
New per-game behaviour belongs in the adapter's shared state; the runner only reads the shared fields, so every adapter gets the same tracking.

In the code: play.py `what_changed`, `Runner._baseline`/`_milestones`/`_reached`/`brief`; memory.py `rom_title`, `describe_knowledge`.
