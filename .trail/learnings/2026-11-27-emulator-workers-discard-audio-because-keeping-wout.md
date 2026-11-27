---
title: "Emulator workers discard audio, because keeping it fills the sandbox memory limit in minutes"
author: Wout
date: 2026-11-27
branch: fix-worker-audio-memory
id: l-8d0747f5
scope: repo
status: active
src: agent
touches:
  - docs/architecture.md
  - src/pokeagent/emulator/worker.py
  - tests/test_emulator.py
---

## Decided
The emulator worker passes libretro.py an audio driver that drops every sample. Nothing in pokeagent plays or analyses sound.

## Tried and ruled out
- Suspected a pipe deadlock on the worker's stderr first: ruled out, stderr goes to /dev/null.
- The real cause: libretro.py's default ArrayAudioDriver appends every sample to an in-memory array. SameBoy outputs 2,097,152 Hz stereo, about 8 MB per emulated second, so the first live session's worker reached the sandbox's 2 GB RLIMIT_AS after roughly four emulated minutes, stopped answering, and was killed at the client's 60 s call deadline (exit -9) in the middle of a battle.

## Watch out
- Any new libretro Session (dev worker, validation worker, a future core) must pass a discarding audio driver too; the default looks harmless in short tests and only fails in long runs.
- A test that measures the worker's resident memory over 15 emulated seconds catches a regression in about 3 seconds.

In the code: src/pokeagent/emulator/worker.py (SilentAudio), tests/test_emulator.py (test_worker_memory_stays_flat_while_running).
