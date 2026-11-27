---
title: Stuck restore only uses checkpoints of the current ladder instance
author: Wout
date: 2026-11-27
branch: phase7-stuck
cost: { minutes: 1, tokens: 811 }
id: l-e14794c0
scope: repo
status: active
src: agent
touches:
  - src/pokeagent/stuck.py
  - src/pokeagent/play.py
---

## Decided
A level 4 restore only considers checkpoints saved by the current ladder instance (the checkpoint event records the ladder id and best score), because scores from before a resume count tiles from a different set and are not comparable.

## Watch out
In the code: src/pokeagent/stuck.py Ladder.id, src/pokeagent/play.py Runner._restore_progress.
