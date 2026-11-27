---
title: "The play prompt only describes macros and their cost, and the agent built its own grinding macros"
author: Wout
date: 2026-11-27
branch: phase6-declarative
id: l-74821456
scope: repo
status: active
src: agent
touches:
  - docs/architecture.md
  - src/pokeagent/prompts/play.md
  - src/pokeagent/server.py
---

## Decided
The play prompt explains define_observation/define_macro/run_macro and their cost (one action per macro call, up to 200 actions inside) without naming any capability to build. Wout's rule: never name a capability in a prompt or manufacture an improvement to satisfy a test.

## Tried and ruled out
Nothing scripted. In the first live session with the tools (2026-11-27, run 1a copy on Route 2) the agent defined 7 macros on its own (grind_one: pace in grass until a battle then fight until it ends; a Pokecenter round trip; run_away), redefined two as version 2 and called them 48 times with no failure.

## Watch out
- Macros that call other macros must be defined after them: define_macro refuses unknown macro names, and the agent hit that once.
- It used mem_read but defined no observation in that session, so observation use is still unproven live.
- The agent's notes still claim inputs get dropped in menus; earlier input tests showed every press is accepted, so that belief is the agent's misreading, not a harness bug.

In the code: src/pokeagent/prompts/play.md ("Your own tools"), src/pokeagent/server.py (define_macro, run_macro).
