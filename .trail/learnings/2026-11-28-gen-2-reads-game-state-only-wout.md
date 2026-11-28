---
title: Gen 2 reads game state only while SVBK selects WRAM bank 1
author: Wout
date: 2026-11-28
branch: gen2
id: l-262a6531
scope: repo
status: active
src: agent
touches:
  - AGENTS.md
  - README.md
  - docs/architecture.md
  - src/pokeagent/games/__init__.py
  - src/pokeagent/games/pokemon_gen1.py
---

## Decided
The game state lives in WRAM bank 1 (D000-DFFF). The worker's read shows whichever bank FF70 & 7 selects there (0 means 1), and banks 2-7 sit at 0x10000+. state() zeroes bank-1 reads when FF70 & 7 is not 0 or 1 (title screens use banks 5 and 6), so power-on and intro never look like a game.
Dialog box rows 12-17, the waiting arrow is on the bottom border at (18,17) (Gen 1: row 16). Yes/no box sits at cols 14-19. screen_text decodes only inside boxes (corners 0x79 0x7B 0x7D 0x7E) and skips picture boxes with no blank in six rows.
Four collision bytes per block, order top-left, top-right, bottom-left, bottom-right of its 2x2 step squares, looked up in CollisionPermissionTable (0 walk, 1 water, 0xF wall, 0x10 talk bit).
Ledges 0xA0-0xA7 are LAND: a step onto one is a plain step, and the next step in the hop direction jumps two squares over the wall after it (verified on Routes 29 and 30). Entering from the far side is blocked by that wall, not by a rule.
SRAM is 32 KiB. Check value 99 at 0x2008, data from 0x2009, little-endian 16-bit sum, check value 127. Gold/Silver: data ends 0x2D69 (checksum 0x2D69, value 0x2D6B). Crystal: data ends 0x2B83, checksum 0x2D0D, value 0x2D0F. Player name at 0x200B. Verified with real in-game saves.

## Watch out
SameBoy accepts Game Boy Color and Game Boy model values; Crystal is colour-only and shows an error under "Game Boy". Use sameboy_rtc "accurate" for deterministic clocks.
Tile ids share the charmap range, so map tiles and Pokemon pictures decode as letters (PKMN on a house row, AHOV:dk in the starter preview). Menus drop presses shorter than about four frames: press() holds four.
Gen 2 charmap differs from Gen 1: 'd 'l ... 'v are 0xD0-0xD6, e-acute is 0xEA, 0xBA-0xBF are kana; letters, digits, space, cursor 0xED and arrow 0xEE are the same.
Warp carpets and doors trigger on a step in their direction after standing on them. The walking field of the player object is 0xFF when standing; a warp starts about 10 frames after the step ends and the player may walk out by itself about 20 frames after the map loads.
A save imported with --sav has no .rtc: the game continues at a fixed Sunday morning on emulated time with no clock prompt.

## Tried and ruled out
Reading the source as "jump when stepping onto the ledge tile, landing past it" (wrong: the player stood on the ledge tile after one step). Side walls 0xB0-0xB7 follow the pret code (block entry going down for low bits 2,6,7; up 3,4,5; right 1,5,7; left 0,4,6) but were not seen on any map tested.
