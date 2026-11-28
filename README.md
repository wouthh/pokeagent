# pokeagent

An environment in which an AI agent plays emulated games on its own, towards an objective you give in plain
language. It keeps what it learns, and it builds and validates its own tools when that helps it reach the objective.

- **Agent:** the local [Claude Code](https://code.claude.com) CLI (`claude -p`), running on your own login.
- **Emulation:** unchanged upstream [libretro](https://www.libretro.com) cores, loaded in-process through
  [libretro.py](https://github.com/JesseTG/libretro.py). SameBoy is used for Game Boy and Game Boy Color; GBA (mGBA)
  and DS (melonDS DS) come later.
- **Supported games:** Pokémon Red, Blue and Yellow (English), and hacks built on their engine such as Blue Kaizo
  (found by scanning the ROM; Brown is expected to work but is untested). Japanese Red/Green/Blue need their own
  ROMs and a kana charmap and are not supported yet.
- **First use case:** Pokémon Red/Blue. The first long-term benchmark is finishing a whole
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
   (`--no-watch` plays without it). `--web 8765` also shows the same on a read-only web page,
   `http://127.0.0.1:8765/` (see below for other devices). `play.log` in the run folder has every message, tool call
   and session summary (`tail -f` it), and `notes.md` the agent's own notes.
4. Continue with `uv run pokeagent resume <run id>`. Without `--max-sessions` it plays until the objective
   is met, the run pauses for you, or you press Ctrl-C (which saves a checkpoint first). When usage limits are hit,
   it waits for the reset.
5. Look at what it did: `pokeagent status <run id>`, `pokeagent stats <run id>` and `pokeagent capability list
   <run id>`. `pokeagent view <run id>` shows the game where it stopped, `pokeagent view <run id> --keyboard` lets you
   play on from there yourself, and `pokeagent export-sav <run id>` writes it as an ordinary `.sav` to load in any
   Game Boy emulator (see below). Problems the agent finds in pokeagent itself go to
   `~/.local/share/pokeagent/harness-issues.md`.

## Playing

```sh
uv run pokeagent play --rom /path/to/your/red.gb              # new run towards the default objective
uv run pokeagent play --rom /path/to/your/red.gb --objective "Get the first badge" \
  --success '{"obs": "badges", "op": ">=", "value": 1}'           # your own check completes the run
uv run pokeagent resume 20261127-170000                       # continue a run where it stopped
uv run pokeagent restore 20261127-170000                      # list the checkpoints a stopped run can go back to
uv run pokeagent restore 20261127-170000 1764263000000000000.state  # go back; learned knowledge stays
uv run pokeagent runs                                         # every run and where its game stands
uv run pokeagent status 20261127-170000                       # where a run stands
uv run pokeagent view 20261127-170000                         # show the game where it stopped (or at a checkpoint)
uv run pokeagent view 20261127-170000 --keyboard --out mine.sav  # play on yourself; keep your in-game save
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

`view --keyboard` plays a copy of the run's game in the window: arrow keys move, X is A, Z is B, Enter is START and
Backspace is SELECT. There is no sound (pokeagent's emulator drops audio). The run does not change; with `--out`, a
save you make in game is written there as a `.sav` when you close the window (`--force` replaces an existing file).
To carry on with sound, open such a `.sav` in another emulator; keep it in its own folder next to a copy of the ROM,
so it cannot replace a save of yours with the same name. `play --sav` lets the agent continue it.

`--web [HOST:]PORT` on `play`, `resume` and `view` serves a read-only page with the live game screen (about 10
pictures a second), the last action and the run panel; it has no controls. By default only this machine can open
it. `--web 0.0.0.0:8765` listens on your network too, for a phone or another computer: then anyone on that network
who can reach the port sees the page, and the firewall must let the port in (on Fedora,
`sudo firewall-cmd --zone=<your Wi-Fi zone> --add-port=8765/tcp`, which lasts until firewalld reloads or the
machine restarts).

`pokeagent web [[HOST:]PORT] [--data-dir DIR]` (default port 8765) serves a read-only browser of every run: a list
with each run's state and where its game stands, and per run the status panel, the latest frame (refreshed every
couple of seconds while the run plays) and its newest 24 checkpoints. Frames are drawn on demand from the `.state`
files the run already keeps and held in memory only, so browsing writes nothing to disk. The same address, network
and firewall notes apply. The window and the pages scale the game screen to fill their space.
When a problem keeps costing actions, the agent may ask for a development session, which writes and validates a
code tool away from the live game. `--dev-share` (default 0.3 of the run's tokens), `--dev-attempts` (2 per tool),
`--dev-timeout` (45 minutes) and `--dev-validations` (10 per session) on `play` and `resume` limit them; the first
one always runs.

Each run lives in `~/.local/share/pokeagent/runs/<id>/`: `play.log` shows every message and action, `notes.md` the
agent's own notes. Ctrl-C stops a run after saving a checkpoint; closing the game window only stops watching.
When the agent makes no progress for many actions, pokeagent escalates from notices to a pause for reflection, a new
session, a restore of the last checkpoint after its progress and finally stops the run as `stuck`; `resume`
continues it.

### Play style and other objectives

New runs play in the natural style: the agent catches species it does not own yet, builds a balanced team in which
no two members share a type, trains it evenly, uses its items (TMs, PP Ups, evolution stones), leaves Pokémon in the
PC when needed, nicknames its team and takes up side quests. Each run draws its own variety once and keeps it in
`run.json`: the starter the fewest earlier runs had, three team types besides the starter's, a nickname theme and
three side quests. `--style direct` pursues the objective by the shortest route instead.

`--glitches` (off by default) allows the game's known glitches done with ordinary controller input; on Red/Blue
the brief names the Mew glitch, MissingNo. and item duplication and arbitrary code execution with item 8F. A
glitch can reach the Hall of Fame check too, as in a glitched speedrun.

`--from RUN` starts a new run from another run's latest game instead of power-on, and `--rom ROM --sav FILE`
starts one from an in-game save made in any emulator (the agent picks CONTINUE). Neither the run nor the file
changes:

```sh
uv run pokeagent play --from 20261127-205537 --objective "Train the whole team evenly to level 50"
uv run pokeagent play --rom red.gb --sav "my red.sav" --objective "Complete the Pokédex"
uv run pokeagent play --from 20261127-205537 --glitches \
  --objective "Raise a Tauros with Smogon's RBY OU set: its moves, maximum stat exp and good DVs"
```

`get_state` shows each party member's stats, stat exp (EVs), DVs (IVs) and the PP Ups on each move, so the agent
can work on objectives like these. pokeagent cannot check them itself, so the agent reports them complete and you
confirm. The agent may also suggest objectives of its own for later runs; `pokeagent status` lists them.

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
`POKEAGENT_ROM` set, the adapter tests also boot a new game and walk out of the player's house; Yellow, Blue Kaizo
and the Gen 2 dumps there add the version and scanner tests.

## Licence

Apache-2.0, see [LICENSE](LICENSE). Pokémon is a trademark of Nintendo, Creatures and GAME FREAK; this project is
not affiliated with them.
