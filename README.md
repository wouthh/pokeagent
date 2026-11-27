# pokeagent

An environment in which an AI agent plays emulated games on its own, towards an objective you give in plain
language. It keeps what it learns, and it builds and validates its own tools when that helps it reach the objective.

- **Agent:** the local [Claude Code](https://code.claude.com) CLI (`claude -p`), running on your own login.
- **Emulation:** unchanged upstream [libretro](https://www.libretro.com) cores, loaded in-process through
  [libretro.py](https://github.com/JesseTG/libretro.py). SameBoy is used for Game Boy and Game Boy Color; GBA (mGBA)
  and DS (melonDS DS) come later.
- **First use case:** Pokémon Red/Blue (English releases). The first long-term benchmark is finishing a whole
  playthrough without help.

> **Status: early development.** The repository is being built in phases; see
> [docs/architecture.md](docs/architecture.md) for the design and what is implemented so far.

## How it works

The agent never presses buttons one at a time. It calls tools such as "walk to (12, 8)" or "advance the text";
deterministic code turns those into frame-exact controller input and runs the emulator only inside those calls. The
agent reads the game state from RAM (map, position, party, bag, battle, on-screen text) as well as from screenshots.

On top of that loop:

- **Objectives** are persistent and checked against game state where possible.
- **Memory** (SQLite) separates the objective, goals, game progress, world and technical knowledge, strategies and
  lessons, and keeps confirmed facts apart from hypotheses.
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

## Playing

```sh
uv run pokeagent play --rom /path/to/your/red.gb --watch      # new run towards the default objective
uv run pokeagent play --rom /path/to/your/red.gb --objective "Get the first badge" \
  --success '{"obs": "badges", "op": ">=", "value": 1}'           # your own check completes the run
uv run pokeagent resume 20261127-170000 --watch               # continue a run from its latest checkpoint
```

Each run lives in `~/.local/share/pokeagent/runs/<id>/`: `play.log` shows every message and action, `notes.md` the
agent's own notes. Ctrl-C stops a run after saving a checkpoint; closing the `--watch` window only stops watching.

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
