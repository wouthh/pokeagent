---
title: Tool budgets cover nested macros and old capability tables need a migration
author: Wout
date: 2026-11-27
branch: phase6-validation
cost: { minutes: 1, tokens: 1122 }
id: l-ff4cc967
scope: repo
status: active
src: agent
touches:
  - src/pokeagent/extensions.py
---

## Decided
In `src/pokeagent/extensions.py` a macro called from a tool spends the tool budget per action, because passing game._do let 200 presses x10 cost one input. `_contract` in `validation.py` resets scratch stores, because agent tests could set flags that made the trusted run pass.

## Watch out
In `capabilities.py`, CREATE TABLE IF NOT EXISTS never widens CHECK constraints, otherwise old data dirs fail with IntegrityError: a schema change needs a migration.
