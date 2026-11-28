---
title: "Checkpoints record a progress revision, not the best score, for the stuck restore"
author: Wout
date: 2026-11-28
branch: battle-progress
cost: { minutes: 2, tokens: 45594 }
id: l-a8708a25
scope: repo
status: active
src: agent
---

## Decided
Checkpoint events carry the ladder's `revision` (counts every progress event, damage included) as `progress`; the level 4 restore matches on it instead of the best score.
## Watch out
Battle progress resets the ladder without raising the best score, so matching on best would restore a checkpoint from before the damage. In the code: Ladder.revision in stuck.py, Runner.checkpoint/_restore_progress in play.py.
