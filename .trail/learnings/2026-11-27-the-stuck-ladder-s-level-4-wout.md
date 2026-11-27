---
title: "The stuck ladder's level 4 is the second way a timeline branches, besides an explicit restore"
author: Wout
date: 2026-11-27
branch: phase7-stuck
id: l-815f91fb
scope: repo
status: active
src: agent
touches:
  - src/pokeagent/play.py
  - src/pokeagent/stuck.py
---

## Decided
Since phase 7b a timeline branches in two ways: an explicit `pokeagent restore`, and stuck ladder level 4, which restores the first checkpoint saved after the last progress (the plan's ladder step). This updates the phase 5 learning that said only an explicit restore branches. Resume still restores nothing.

## Watch out
- The level 4 restore uses the same `restore()` as the CLI, so it goes back only along the current timeline's history and undoes milestones, progress knowledge, notes and tkv the same way.
- Checkpoint events carry the ladder's best score ("best"); the target is the earliest restorable checkpoint with the ladder's current best. Scores from before a resume are not comparable, because the ladder (and its tile set) starts over on resume.

In the code: src/pokeagent/play.py (Runner._restore_progress, Runner.loop), src/pokeagent/stuck.py.
