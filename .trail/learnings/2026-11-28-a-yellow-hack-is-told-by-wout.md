---
title: "A Yellow hack is told by the colour flag, not its header title"
author: Wout
date: 2026-11-28
branch: gen1-variants
cost: { minutes: 1, tokens: 52997 }
id: l-aa202028
scope: repo
status: active
src: agent
---

## Decided
Gen1 treats a ROM as Yellow-derived when its header title has YELLOW or the CGB flag (0x143 bit 7) is set; Red/Blue are 0x00.
## Watch out
check_layout only fixes WRAM addresses; starters and map names are chosen at construction, so detection must be right up front.
