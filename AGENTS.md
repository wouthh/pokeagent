# pokeagent agent guide

Policy: `implementation-review-loop-v1`. Default branch `main`. The repository is public: every tracked file stays
generic (no personal data, no ROMs, no game data, no private paths).

## Purpose

pokeagent lets an AI agent (the local Claude Code CLI first) play emulated games autonomously towards a
natural-language objective, keep what it learns, and build, validate and use its own tools. Pokémon Red/Blue is the
first use case, not an architectural constraint. Design and decisions: [docs/architecture.md](docs/architecture.md).

## Repository map and instruction precedence

- `src/pokeagent/`: the package; `cli.py` is the entry point (`pokeagent`, also `python -m pokeagent`).
- `tests/`: pytest suite; runs without any ROM. ROM-dependent tests skip unless `POKEAGENT_RED_ROM` points to a dump.
- `scripts/check.sh`: the full local gate. `.prloop.json`: review-loop config.
- `docs/architecture.md`: components, isolation, data boundaries, verified spike facts and the Decisions log.
- `.github/workflows/ci.yml`: lint and ROM-free tests on push/PR. Informational; not a prloop gate.
- No nested AGENTS.md files.

## Protected state

- Never commit ROMs, saves (`.sav`, `.srm`), save states (`.state`), RAM dumps, run data, logs or agent-built
  capabilities. The gate refuses tracked ROM/save/state files; the only allowed `.gb` is the MIT test ROM
  `tests/fixtures/dmg-acid2.gb`.
- Runtime data lives outside the repository in `~/.local/share/pokeagent/` (`--data-dir`), cores in
  `~/.cache/pokeagent/cores/`. Never point tests or tools at a real user's data directory; tests use `tmp_path`.
- Never log or commit MCP session tokens, Claude credentials or anything from `~/.claude`.
- Preserve uncommitted work in a checkout; never reset, stash or clean to fit a workflow.

## Change boundaries

- Ordinary work: package code, tests, docs, prompts.
- Needs Wout's approval first: weakening the sandbox or the tool restrictions for Claude sessions, giving agent code
  a memory-write path or network access, publishing anything automatically, new external hosts contacted by default,
  new runtime dependencies.
- Every script and tool prints nothing when nothing needs to happen; failures and one short line for a requested run
  that finished. Everything else goes to a log file.
- No permanent compatibility code for old state: migrate once instead.

## Architecture and invariants

- The orchestrator enforces every boundary and fails closed: if the sandbox probe or a Claude session's tool
  restrictions cannot be established, nothing runs.
- Agent-written code never runs in the orchestrator process; it runs only in bubblewrap sandboxes (no network, no
  home, read-only system and ROM).
- Game progress happens only through controller inputs. RAM access is read-only, and only for RAM regions.
- The run's objective and its authoritative success checks are immutable; agent-proposed checks never complete a run.
- Learned knowledge and capabilities survive a checkpoint restore; timeline-bound progress (goals, milestones, notes,
  progress knowledge) does not.

## Validation

```text
Focused check:   uv run pytest -q tests/<file>.py
Full local gate: scripts/check.sh
ROM checks:      POKEAGENT_RED_ROM=/path/to/your/dump.gb uv run pytest -q
```

The gate needs uv (Python 3.13 is fetched by uv). It runs ruff check, ruff format --check and pytest, and fails on
tracked ROM/save/state files. Measured 2026-11-27 (bootstrap): 0.7 s wall time, one core, 45 MB RSS. Re-measure
when the gate changes. CI runs the same gate without a ROM; prloop does not wait on it.

## Delivery

- Feature branch from `main`, normal commits, ready PR, review loop with `prloop`
  (`prloop run --repo wouthh/pokeagent --pr <N> --reviewer auto --merge rebase --deadline <dur>`, from the main
  checkout). `.prloop.json` declares the light gate, rebase merges and the `setup` command. Merge by rebase only.
- Once a PR exists, run the full gate only through `prloop ready`.
- Keep README, this file, `docs/architecture.md` (including its Decisions log) and tests current with each change.
- Run `trail check` before committing, pushing or opening a PR; commit `.trail/learnings/` with the change.

## Code Review Rules

- Any path that lets agent-written code run outside the bubblewrap sandbox, reach the network, read outside its
  mounts, or write emulator memory is a blocking finding.
- Any change that lets a Claude session use tools beyond pokeagent's MCP server, WebSearch and WebFetch, or that
  stops the orchestrator from failing closed, is a blocking finding.
- ROM bytes, save data, tokens or personal data in tracked files, logs meant for publication, or test fixtures (other
  than the MIT `dmg-acid2.gb`) are a blocking finding.
