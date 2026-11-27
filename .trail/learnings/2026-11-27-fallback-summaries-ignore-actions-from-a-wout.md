---
title: Fallback summaries ignore actions from a restored-over attempt
author: Wout
date: 2026-11-27
branch: phase5-objectives
cost: { minutes: 1, tokens: 49017 }
id: l-6c508bcb
scope: repo
status: active
src: agent
---

## Decided
A restore writes a `restore` event; fallback session summaries read only actions after the latest one, so abandoned attempts (same session number) never reach the brief.

## Watch out
Timelines are never advanced on restore, so filtering events by timeline does not work. In the code: play.py restore_context, loop.
