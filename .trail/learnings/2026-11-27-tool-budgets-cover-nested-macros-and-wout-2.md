---
title: "Tool budgets cover nested macros, and a schema change gets a migration only when a real data dir holds the old schema"
author: Wout
date: 2026-11-27
branch: phase7-develop
id: l-1be58df7
scope: repo
status: active
replaces: l-ff4cc967
src: agent
touches:
  - docs/architecture.md
  - src/pokeagent/capabilities.py
  - src/pokeagent/extensions.py
  - src/pokeagent/validation.py
---

## Decided
A macro called from a tool spends the tool budget per action, because passing game._do let 200 presses x10 cost one input. The trusted contract runs in validation reset the scratch stores, because the agent's tests could set flags that made the trusted run pass.
The one-time rebuild of the capabilities table (for 6b's wider kind and status checks) was removed: Wout's rule is no compatibility code for old states unless a real environment holds them, and none did (the real data dir had no memory.db yet; only scratch copies of live checks had the 6a table).

## Watch out
CREATE TABLE IF NOT EXISTS never widens CHECK constraints. Before changing a constraint, check the real data dir (~/.local/share/pokeagent/memory.db): if it holds the old table, write a one-time migration; if not, just change the schema. Scratch copies can be rebuilt by hand.

In the code: src/pokeagent/extensions.py (macro calls from tools), src/pokeagent/validation.py (fresh stores per contract run), src/pokeagent/capabilities.py (SCHEMA).
