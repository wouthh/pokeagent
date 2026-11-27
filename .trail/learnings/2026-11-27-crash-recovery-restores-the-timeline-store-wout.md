---
title: "Crash recovery restores the timeline store too, and shared tools recheck ROM compatibility"
author: Wout
date: 2026-11-27
branch: phase6-validation
cost: { minutes: 1, tokens: 67793 }
id: l-7ec65f5e
scope: repo
status: active
src: agent
---

## Decided
The timeline store (tkv.json) is snapshotted with recovery.state after every action and put back by recover(), so a restart cannot leave a tool flag describing an undone action.

## Watch out
A family-scoped tool is visible to other ROMs; Runner.call refuses it when compatibility.roms excludes the current ROM. Invariants are also checked when a nested request fails.

In the code: play.py _after_action/recover, extensions.py Runner.call/_serve.
