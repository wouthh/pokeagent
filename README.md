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
uv run pokeagent --help
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

Runs ruff (lint and format) and the test suite, and fails if any ROM, save or state file is tracked. No ROM is
needed.

## Licence

Apache-2.0, see [LICENSE](LICENSE). Pokémon is a trademark of Nintendo, Creatures and GAME FREAK; this project is
not affiliated with them.
