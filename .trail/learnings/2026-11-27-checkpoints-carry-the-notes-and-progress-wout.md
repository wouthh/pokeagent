---
title: Checkpoints carry the notes and progress with them
author: Wout
date: 2026-11-27
branch: phase4-play-loop
cost: { minutes: 1, tokens: 54255 }
id: l-fb90a855
scope: repo
status: active
src: agent
---

## Decided
Each checkpoint copies notes.md and state.json beside the emulator state; resume restores them, so a rolled-back timeline never keeps newer notes or summaries. Run dirs are resolved to absolute paths because the Claude child's cwd is the run dir.
## Watch out
In the code: Runner.checkpoint and Runner.restore_context in play.py; pruning old checkpoints must delete the sidecar files too.
