# pokeagent

An environment in which an AI agent plays emulated games on its own, towards an objective you give in plain
language. It keeps what it learns, and it builds and validates its own tools when that helps it reach the objective.

- **Agent:** the local [Claude Code](https://code.claude.com) CLI (`claude -p`), running on your own login.
- **Emulation:** unchanged upstream [libretro](https://www.libretro.com) cores, loaded in-process through
  [libretro.py](https://github.com/JesseTG/libretro.py). SameBoy is used for Game Boy and Game Boy Color; GBA (mGBA)
  and DS (melonDS DS) come later.
- **First use case:** Pokémon Red/Blue (English releases). The first long-term benchmark is finishing a whole
  playthrough without help.

> **Status:** the play loop, memory and timelines, the agent's own observations, macros and validated code tools,
> develop mode, the stuck ladder and run metrics are implemented (phases 1-7). Long live runs are the current work;
> see [docs/architecture.md](docs/architecture.md) for the design and what has been observed live.

## How it works

The agent never presses buttons one at a time. It calls tools such as "walk to (12, 8)" or "advance the text";
deterministic code turns those into frame-exact controller input and runs the emulator only inside those calls. The
agent reads the game state from RAM (map, position, party, bag, battle, on-screen text) as well as from screenshots.

On top of that loop:

- **Objectives** are persistent and checked against game state where possible.
- **Memory** (SQLite) keeps the objective and its checks, game progress, world and technical knowledge, strategies
  and lessons, and keeps confirmed facts apart from hypotheses. Progress belongs to a timeline, so going back to a
  checkpoint undoes it while what was learned stays.
- **Capabilities** grow during the run. The agent can define RAM observations and macros while playing, and write
  code extensions in a separate development mode. Every capability is validated against trusted contracts in an
  isolated emulator before it is activated, and is versioned and reversible.
- **Isolation:** agent-written code runs only in a [bubblewrap](https://github.com/containers/bubblewrap) sandbox
  without network or home access, and Claude sessions can act only through pokeagent's own tools.

## Requirements

- Linux x86_64, [uv](https://docs.astral.sh/uv/), `bwrap` (bubblewrap) and `7z` (p7zip)
- The [Claude Code](https://code.claude.com) CLI, logged in with `claude auth login`
- Your own legally obtained ROM dump. **No ROMs, saves or game data are included or ever committed.**

## Setup

```sh
uv sync
uv run pokeagent fetch-cores   # once: downloads RetroArch's pinned stable cores archive (274 MB), keeps only
                               # the checksummed cores in ~/.cache/pokeagent/cores/ and deletes the archive
uv run pokeagent --help
```

Emulator cores always run in a bubblewrap sandbox; if the sandbox cannot be set up (for example when unprivileged
user namespaces are disabled), pokeagent refuses to start rather than running unsandboxed.

## Trying it

1. Check that the Claude CLI is logged in: `claude auth status`.
2. Start a short run you can watch:

   ```sh
   uv run pokeagent play --rom /path/to/your/red.gb --max-sessions 1
   ```

   A session is one `claude -p` conversation of at most 100 game actions. In the live checks so far a session took
   3 to 7 minutes and cost $1 to $2 of Claude usage (reported per session in `play.log` and by `pokeagent stats`).
   `--model` and `--effort` choose the Claude model and reasoning effort.
3. Watch it: a window opens with the game as it plays, the last action and a panel with the run's status
   (`--no-watch` plays without it). `play.log` in the run folder has every message, tool call and session summary
   (`tail -f` it), and `notes.md` the agent's own notes.
4. Continue with `uv run pokeagent resume <run id>`. Without `--max-sessions` it plays until the objective
   is met, the run pauses for you, or you press Ctrl-C (which saves a checkpoint first). When usage limits are hit,
   it waits for the reset.
5. Look at what it did: `pokeagent status <run id>`, `pokeagent stats <run id>` and `pokeagent capability list
   <run id>`. `pokeagent view <run id>` shows the game where it stopped, and `pokeagent export-sav <run id>` writes
   it as an ordinary `.sav` to load in any Game Boy emulator (see below). Problems the agent finds in pokeagent itself go to `~/.local/share/pokeagent/harness-issues.md`.

## Playing

```sh
uv run pokeagent play --rom /path/to/your/red.gb              # new run towards the default objective
uv run pokeagent play --rom /path/to/your/red.gb --objective "Get the first badge" \
  --success '{"obs": "badges", "op": ">=", "value": 1}'           # your own check completes the run
uv run pokeagent resume 20261127-170000                       # continue a run where it stopped
uv run pokeagent restore 20261127-170000                      # list the checkpoints a stopped run can go back to
uv run pokeagent restore 20261127-170000 1764263000000000000.state  # go back; learned knowledge stays
uv run pokeagent status 20261127-170000                       # where a run stands
uv run pokeagent view 20261127-170000                         # show the game where it stopped (or at a checkpoint)
uv run pokeagent export-sav 20261127-170000                   # write it as a .sav for other emulators
uv run pokeagent stats 20261127-170000 --compare 20261201-090000  # per-session metrics, compared at milestones
uv run pokeagent capability list 20261127-170000              # the observations, macros and tools it defined
uv run pokeagent capability rollback 20261127-170000 heal     # take one back to its previous version
```

`export-sav` saves the game the way a player would, on a copy, so the run itself is not touched: it closes open
text, backs out of menus with B and saves through the START menu. It uses the newest state that allows that (not one
in a battle or at a yes/no prompt), says which, and writes the cartridge's 32 KB battery RAM, the standard `.sav`
format that other emulators (mGBA, SameBoy, BGB, PyBoy and others) load next to the ROM. It goes to
`<ROM name>.sav` in the run folder unless you pass `--out`, and never replaces a file without `--force`. Give a
checkpoint name (from `pokeagent restore <run id>`) to export or view that one instead.

When a problem keeps costing actions, the agent may ask for a development session, which writes and validates a
code tool away from the live game. `--dev-share` (default 0.3 of the run's tokens), `--dev-attempts` (2 per tool),
`--dev-timeout` (45 minutes) and `--dev-validations` (10 per session) on `play` and `resume` limit them; the first
one always runs.

Each run lives in `~/.local/share/pokeagent/runs/<id>/`: `play.log` shows every message and action, `notes.md` the
agent's own notes. Ctrl-C stops a run after saving a checkpoint; closing the game window only stops watching.
When the agent makes no progress for many actions, pokeagent escalates from notices to a pause for reflection, a new
session, a restore of the last checkpoint after its progress and finally stops the run as `stuck`; `resume`
continues it.

The default objective completes when the game reaches the Hall of Fame. A custom objective completes only with your
`--success` check. Without one, the agent may propose a check or report the objective complete, and the run then
pauses for you:

```sh
uv run pokeagent objective confirm 20261127-170000                     # the objective is met: complete the run
uv run pokeagent objective reject 20261127-170000 --reason "not yet: ..."  # the agent reads why, then resume
uv run pokeagent objective approve-check 20261127-170000               # make its proposed check authoritative
```

## What stays local and what leaves your machine

- **Local only:** the ROM, save states, the database, logs, the agent-built capability library and harness issues,
  all under `~/.local/share/pokeagent/` (cores are cached in `~/.cache/pokeagent/`).
- **Sent to Anthropic:** everything the model sees, through the Claude CLI on your account: prompts, tool results,
  screenshots, RAM-derived text, notes and knowledge.
- **Sent to third parties:** web searches and fetches the agent makes while researching, from your machine.
- Nothing is published anywhere automatically.

## Checks

```sh
scripts/check.sh
```

Runs ruff (lint and format), installs the pinned cores if missing, runs the test suite (including sandbox escape
tests and the MIT-licensed [dmg-acid2](https://github.com/mattcurrie/dmg-acid2) test ROM), and fails if any ROM,
save or state file is tracked. No game ROM is needed; with your own Red or Blue dump in `roms/` (ignored) or
`POKEAGENT_ROM` set, the adapter tests also boot a new game and walk out of the player's house.

## Licence

Apache-2.0, see [LICENSE](LICENSE). Pokémon is a trademark of Nintendo, Creatures and GAME FREAK; this project is
not affiliated with them.
