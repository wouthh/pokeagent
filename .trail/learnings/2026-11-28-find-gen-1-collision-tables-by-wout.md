---
title: "Find Gen 1 collision tables by row shape, not embedded ROM bytes"
author: Wout
date: 2026-11-28
branch: gen1-variants
cost: { minutes: 1, tokens: 1465 }
id: l-e79d67ef
scope: repo
status: active
src: agent
---

## Decided
Locate the tile-pair and ledge tables by row shape (ledge facing matches its joypad bit plus 0xFF terminator; longest tileset-triple run in bank 0), never by literal ROM bytes: ROM bytes in tracked files are a blocking finding.
## Watch out
Checked on Red, Blue, Yellow and Blue Kaizo dumps; a hack with other row layouts is not recognised.
