---
title: Crash recovery saves the game after every action instead of replaying an input log; no goal tools
author: Wout
date: 2026-11-27
branch: phase5-timelines
id: l-a0df044d
scope: repo
status: active
src: agent
touches:
  - AGENTS.md
  - README.md
  - docs/architecture.md
  - src/pokeagent/cli.py
  - src/pokeagent/emulator/client.py
  - src/pokeagent/memory.py
  - src/pokeagent/play.py
  - src/pokeagent/server.py
  - tests/test_memory.py
  - tests/test_play.py
---

## Decided
Wout chose (2026-11-27) to drop the planned input log, RAM-hash replay and `replay` command. After every action the runner writes `recovery.state` (about 250 KB, milliseconds); resume and automatic emulator restarts load it, so at most the interrupted action is lost and nothing is ever replayed, which also rules out replaying an abandoned timeline. Wout also chose no goal tools: the agent's plan lives in its notes and `next_focus`, which travel with checkpoints; milestones are to be derived from game state in phase 7.

## Tried and ruled out
Phase 5a's review loop bolted timeline rules onto resume (refuting progress after the latest checkpoint on every resume, a `restore` event that summaries skipped). Those stopgaps are gone: resume restores nothing, and only an explicit `pokeagent restore` branches a timeline.

## Watch out
Restores only go back along the current timeline's history; restoring into an abandoned branch would need to revive progress knowledge an earlier restore made stale. The action event is recorded after the recovery state is saved, so an action undone by an emulator restart never becomes evidence.

In the code: Runner._after_action, Runner.recover, latest_state, restore and restorable in play.py; Memory.history and Memory.branch in memory.py; GameServer on_game and act in server.py.
