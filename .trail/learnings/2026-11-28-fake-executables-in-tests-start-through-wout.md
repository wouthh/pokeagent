---
title: "Fake executables in tests start through a shell wrapper, not a shebang to the venv python"
author: Wout
date: 2026-11-28
branch: test-shebang-spaces
id: l-99f57eb5
scope: repo
status: active
src: agent
touches:
  - tests/test_claude_cli.py
---

## Decided
Test stand-ins for external programs (the fake `claude`) are a small `/bin/sh` wrapper that `exec`s `sys.executable` on a separate script, with both paths quoted.

## Tried and ruled out
`#!{sys.executable}` as the shebang: the kernel splits a shebang at whitespace, so in the main checkout ("Coding projects") it tries to execute the directory `/home/wout/Documents/Coding` and every test fails with PermissionError.

## Watch out
prloop gates run in /tmp worktrees without spaces, so this only fails when the suite runs from the main checkout. In the code: tests/test_claude_cli.py `fake_claude`.
