---
title: Unevaluable tool invariants are contract violations and validation runs start from a copy of the live timeline store
author: Wout
date: 2026-11-27
branch: phase6-validation
cost: { minutes: 1, tokens: 53766 }
id: l-870b2ff7
scope: repo
status: active
src: agent
---

## Decided
An invariant that cannot be evaluated (type mismatch, observation missing on this ROM) raises a violation, not a catchable request error. Trusted validation runs restore a scratch copy of the live timeline store instead of an empty one.
## Watch out
In the code: extensions.py `_check_invariants`, validation.py `_reset_tkv`. Validation must put game.tkv back afterwards.
