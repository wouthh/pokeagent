# Architecture

This document describes the target design, what is implemented so far, and the decisions behind it. Sections marked
*(planned)* are not built yet.

## Goal

pokeagent is an environment in which an agent plays emulated games autonomously towards a natural-language
objective, accumulates knowledge, and improves its own tooling when that helps. The model stays the same; its
harness, knowledge and capabilities evolve. Pokémon Red/Blue is the first use case and the first long-term
benchmark (a full playthrough without manual help).

## Components

```
pokeagent play/resume ── orchestrator (trusted, host)
  ├─ agent provider: ClaudeCliProvider → `claude -p` sessions (mode = play | develop)
  ├─ MCP server 127.0.0.1 (per-session token bound to mode; game-touching calls serialised)
  ├─ capability registry (builtin + declarative + code extensions, versioned, with contracts)
  ├─ memory store (SQLite + FTS5) + run files, timeline-aware
  ├─ stuck detector, policy, budgets, checkpoints, metrics
  ├─ viewer (Tk window and read-only web pages: frame + status panel, run browser; keyboard play on a copy)
  └─ child processes, each in its own bwrap sandbox with minimal mounts:
       live emulator worker   python/venv, package, core .so, ROM (ro); tmpfs
       extension host         python/venv, package, that capability version (ro); tmpfs; no ROM
       validation/dev worker  python/venv, package, core, ROM, staged copy, fixtures (ro); tmpfs
```

Thin interfaces exist only at these seams, each with one implementation for now:

| Interface | v1 implementation |
|---|---|
| `EmulatorBackend` (load, step with buttons, frame, read RAM, save/load state) | libretro.py + SameBoy core |
| `GameAdapter` (identify ROM, observations, state text, walk grid, progress score, predicates) | `pokemon_gen1` |
| `AgentProvider` (`run(SessionSpec) -> SessionResult`, neutral events) | `ClaudeCliProvider` |

## Data: local versus remote

- **Local only** (`~/.local/share/pokeagent/`, override `--data-dir`): ROM path, save states, the SQLite database,
  session streams, logs, the agent-built capability library, harness issues. Cores: `~/.cache/pokeagent/cores/`.
- **Remote (Anthropic, through the Claude CLI on the user's account):** everything the model sees: prompts, briefs,
  tool results, screenshots, RAM-derived text, notes, knowledge hits.
- **Remote (third parties):** WebSearch/WebFetch requests made by the CLI from the host.
- Nothing is published automatically.

## Emulator worker *(implemented, phase 2)*

- `Emulator(core, rom, options=…)` (`emulator/client.py`) starts `pokeagent.emulator.worker` through
  `sandbox.spawn`. The core and ROM are bound read-only at `/in/`, with the default rlimits: 2 GB address space,
  32 processes or threads, 64 MB file size and 256 open files.
- Protocol on the worker's stdin/stdout:
  - every message is one JSON line (at most 1 MB), and `"n": N` means N raw bytes follow (at most 16 MB);
  - the client enforces the caps and a per-call deadline, and kills the process group if either is broken.
- Operations:
  - `info`, `run(buttons, frames)` (hold, then release), `frame` (RGB bytes);
  - `read(addr, n)`: non-const memory-map regions only, so ROM is refused; the regions are taken after the first
    frame, because mGBA publishes its memory map during the first `retro_run`, not at load;
  - `ram_hash`: sha256 of system RAM plus save RAM; on a GBA core, of all of EWRAM and IWRAM plus save RAM, since
    mGBA's `SYSTEM_RAM` id is only the first 32 KiB of EWRAM;
  - `save_ram`: the cartridge's battery RAM, read-only (what a `.sav` file holds);
  - `save_state` / `load_state`.
- There is no write operation. A `.sav` given when the worker starts (`Emulator(..., battery=PATH)`, bound
  read-only) is copied into the cartridge's battery RAM before the first frame, as any emulator loads a save; a
  file that does not fit the cartridge stops the worker.
- Checkpoints and timelines live in the run directory and the memory store (phases 4–5). Running the same inputs
  from the same state gives the same RAM hash and state (tested), although recovery never replays inputs.

## Game adapters

`games/identify(rom)` asks each adapter in `ADAPTERS` whether it recognises the ROM and builds the first that does;
any other ROM is refused with the list of supported games. The contract every adapter follows is in the docstring of
`games/__init__.py`: its core and options, default objective and trusted check, observations for success checks, the
natural style's starters, team types, side quests and glitch text, the shared state shape, and the controller-only
macros. `run.json` records the adapter's family, core and core options, so restores and `--from` check them.

## Game adapter: Pokémon Gen 1 *(implemented, phase 3; Yellow and hacks added later)*

`games/pokemon_gen1.py`, for the English Red, Blue and Yellow and hacks of their engine. Addresses come from the
pret pokered and pokeyellow disassemblies. Pokémon, item and move names, the type names and the collision tables are
read from the user's ROM at runtime, so no game text is stored in the repository; `gen1_map_names.txt` and
`gen1_yellow_map_names.txt` hold only the map constant names.

- **Version from the ROM, not a sha1.** `gen1_tables.find_tables(rom)` locates every table by facts in the ROM (the
  one run of 190 ten-byte name records, the strings after POUND and MASTER BALL, the pointers to NORMAL, FIGHTING and
  FLYING, the row shapes of the tile-pair and ledge tables). `recognises` is true when all six are found, so Blue Kaizo
  works with no entry; `ROMS` only names known dumps (an unknown one is "Gen 1 ROM hack (header title X)"). Yellow
  (header title, or the species table in bank 0x3A) selects its own layout, `STARTERS` (Pikachu only), side quests
  and map names; the first state read of a running game corrects a wrong guess for all of them. The CGB flag picks
  only the colour core options, since Red/Blue hacks may set it too.
- **Layout probe.** `RAM_LAYOUTS` holds Red/Blue's WRAM addresses and Yellow's (one lower from `WALK_COUNTER` on).
  The first `state` of a running game checks the guess against the game's own invariants (name, party and bag
  terminators, map header, tileset pointers) and switches to the layout that fits; if none does the state says the
  layout is unverified instead of refusing the game.
- **Yellow:** Pikachu follows in sprite slot 15, which `grid` skips so it never blocks `walk_to`.

- **State** (`state`): map and position, facing, screen text decoded from the tile map, screen mode
  (overworld, dialog, menu, battle, plus cursor, yes/no and waiting-arrow flags), names, party, bag, money, badges,
  Pokédex counts and the current battle.
- **Walk grid** (`grid`): one cell per player step, built from the current map's blocks, the tileset's blocks and
  collision list, and the lower-left tile of each step square (the tile the game checks). Tile-pair collisions,
  one-way ledges (overworld tileset only), sprites and warps are included; paths come from a BFS.
- **Actions**, controller input only: `press`, `step`/`walk`, `walk_to(x, y)` (re-plans after every step and stops on
  a map change, battle or dialog), `choose(label)` for menus, and `advance_text`, which presses A through dialog but
  never at a menu or yes/no prompt.
- **Scoring and checks:** `fingerprint` (the standard groups for contracts and stuck detection), `progress_score`
  (badges, maps, Pokédex, levels and experience, new tiles), `observations` (trusted values for success checks) and
  the default objective's check (the Hall of Fame map).
- Tests boot a new game on the user's own ROM (`POKEAGENT_ROM`, or a supported dump in the checkout's ignored
  `roms/` folder) and skip without one.

## Game adapter: Pokémon Gen 3 *(Ruby, Sapphire, Emerald, FireRed, LeafGreen; mGBA)*

`games/pokemon_gen3.py` with `games/gen3_tables.py`. RAM addresses come from the pret pokeruby, pokeemerald and
pokefirered symbol files, by family (`rs`, `e`, `frlg`); names come from the user's ROM at runtime.

- **Recognition.** The header game code (AXVE, AXPE, BPEE, BPRE, BPGE) picks the family; `find_tables` finds the
  species, move, item, type and ability names, the natures and the map-name table by the text of their first two
  entries and their stride, and the base stats at the family's fixed distance from the species names, confirmed by
  their shape. `ROMS` lists the dumps checked; an unlisted dump that scans is accepted.
- **RAM layouts** (`LAYOUTS`): fixed save blocks in Ruby and Sapphire, pointers to them in Emerald and FireRed/LeafGreen
  (the pointers move, so every `state` dereferences them). The layout probe checks that `gMain.callback2` is a ROM
  pointer and the save block pointers point into EWRAM; a hack that moved RAM (ROWE) then gets a state that says so
  instead of garbage. Hacks whose names are not in capitals (ROWE, Radical Red) are not recognised at all.
- **Map names.** The header's section (a whole town) is too coarse: every house and the lab are "LITTLEROOT TOWN".
  `gen3_map_names_{rs,e,frlg}.txt` hold pret's map identifiers by group and number (identifiers only, no game text, like
  `gen1_map_names.txt`), shown as "Littleroot Town - Brendans House - 1F". A map whose identifier does not fit the
  section the ROM names (a build that does not match pret) shows as "SECTION (group.number)", as does a map pret
  does not list.
- **No map, no game.** During the intro and the naming screen the save blocks and the name are set but the map header is
  still empty, so `in_game` also needs a loaded map (header pointers in the ROM, a sane grid size in EWRAM); `grid`
  returns an empty grid for an invalid header instead of reading outside the ROM.
- **Pokémon:** `decode_mon` decrypts the four data blocks (order by personality mod 24, XOR with personality and
  trainer id, checksum) and reports species, level, HP, status, moves with PP and PP Ups, experience, stats, EVs, IVs,
  nature, ability, held item and types (from the base stats). Money and bag quantities are XORed with the save block
  key in Emerald and FireRed/LeafGreen. Badges, Pokémon-get and the Hall of Fame (`FLAG_SYS_GAME_CLEAR`, the default
  check) are event flags; the Pokédex counts are popcounts of the owned and seen bits.
- **Screen modes.** `overworld`, `dialog`, `menu`, `battle` with `cursor`, `yes_no` and `waiting` as in Gen 1; the
  text is the paragraph being printed, read through the text printer's next-character pointer (Emerald, FireRed,
  LeafGreen: 32 printers with a state, 1-3 = waiting) or the message window (Ruby, Sapphire: state 5, 8, 9). A message
  that has finished printing still waits for A or B while the script engine's native wait is `WaitForAorBPress` (its
  pointer is stale unless the engine's mode is 2). Menus and yes/no prompts are tasks, and the battle's action and move
  menus are `gBattlerControllerFuncs[0]`: `FUNCTIONS` holds the ROM function addresses of the exact ROMs they were read
  from (pret symbols; the Sapphire dump's found by their opening bytes). Any other ROM, or a hack, falls back to RAM-only
  signals (printer and window states, script mode, `inBattle`), with which `advance_text` presses only on printer
  waits and never answers anything it cannot tell is a prompt.
- **`advance_text` presses B, not A.** Both continue a message, but A talks to the person in front of you again once
  the box has closed. It stops at any menu or yes/no and waits through a running script (a cutscene).
- **`press` waits for the screen.** The game ignores input during a palette fade and while a menu or an animation
  starts, so `press` first waits until the picture has stood still (up to 60 frames; an animated screen is pressed
  anyway), holds 6 frames, waits until the picture reacts and settles again. Every press of `times > 1` lands, and the
  result and screenshot show the settled screen. `advance_text` uses the raw `tap` (a ▼ blinks, so its screen never
  settles); a step that ends on a warp waits for the fade. The Ruby/Sapphire intro's text lives in the menu window,
  which `_printers` reads next to the field's message window. A full-screen menu (the clock) is `menu`, not `dialog`,
  even while its script runs.
- **Text on Ruby and Sapphire** is a window holding the string's start and an index into it, so the next character is
  their sum; the intro's questions (boy or girl, the name) are asked in the menu window and shown as the screen text of
  that menu. In battle, `advance_text` waits (up to 900 frames) through animations for the next message or menu, and
  the result shows the latest battle message from its buffer. `press` also waits for a message to finish printing.
- **Exits.** Only warp events on door, stair, ladder and arrow tiles (behaviours 0x60-0x7F) are drawn as `W` and walked
  to; the maps' other warp events (a second mat tile beside a door, the intro truck's door before it opens) do not fire.
  A step that pushes into the wall from such a tile waits for the warp and reports `map_changed`.
- **Walk grid.** The game's own map grid (`gBackupMapLayout`/`VMap`, with the 7-tile border of the neighbouring maps, so
  a path can cross an edge): collision bits 10-11, the tilesets' metatile behaviours read from the ROM (ledges are
  one-way hops over two tiles, water needs Surf and waterfalls are blocked), people from the object events, warps
  from the map header's events. Walking is the same contract as Gen 1.
- **Saves.** `save_game` goes START, SAVE, YES (the cursor index follows the flags that add Start menu entries);
  `save_complete` checks the 128 KiB flash image: one of the two slots with all 14 sectors, signature, ids and checksums.
- **Worker and tools.** The agent's memory reads and develop snapshots are limited to EWRAM and IWRAM on GBA
  (`RAM_RANGES`); the buttons include L and R.
- Natural play: the three starters of the family, all real types as team types, and side quests in neutral words;
  no glitches are named.
- Tests: `tests/test_gen3.py` builds and encrypts Pokémon, saves and grids from bytes (no ROM), and boots each of the
  five dumps found in `roms/`.
## Game adapter: Pokémon Gen 2 *(Gold, Silver, Crystal and engine hacks)*

`games/pokemon_gen2.py` subclasses `Gen1` and reuses its helpers, `Grid` (as `Gen2Grid`) and its macros (`press`,
`walk`, `walk_to`, `choose`, `advance_text`, `save_game`); what differs is parameterised in `pokemon_gen1.py`
(charmap, tile-map row of the ▼, step and reaction frames) or overridden. Pokémon, item and move names, the type names,
the base data (types) and the collision permission table are read from the user's ROM; `gen2_gold_silver_map_names.txt`
and `gen2_crystal_map_names.txt` hold only pret's map constant names.

- **Recognition.** `gen2_tables.find_tables` scans for every table (the run of 251 ten-byte names, the strings after
  POUND and MASTER BALL, the pointers to NORMAL, FIGHTING and FLYING, the 32-byte base data records numbered 1, 2, 3,
  the permission table's first 32 bytes, the landmark table's first two entries). There is no sha1 gate: `ROMS` only
  names Gold, Silver and Crystal 1.1; any other dump is "Gen 2 ROM (header title X)". The header (title `PM_CRYSTAL` or
  the colour-only flag) picks Crystal's WRAM layout, else Gold/Silver's; the first `state` of a running game checks
  the guess against the game's invariants and says the layout is unverified if none fits (as Gen 1). Crystal 1.0, the
  Australian Crystal and Crystal Clear take this path and are unverified: Crystal Clear's tables were found by the scan
  and its tile map address held, but its custom intro was not passed, so no gameplay was checked.
- **Core.** `{"sameboy_model": "Game Boy Color", "sameboy_rtc": "accurate"}` for all three (Crystal is colour-only; the
  accurate clock keeps checkpoints and fixtures deterministic). A game started from a `--sav` has no `.rtc` file: the
  clock starts at a fixed Sunday morning on emulated time and Continue shows no clock prompt (checked for Gold and
  Crystal).
- **Memory.** The game state is in WRAM bank 1; the worker's `read` shows whichever bank SVBK selects at D000-DFFF, so
  `state` reads the bank-1 values only while FF70 & 7 is 0 or 1 (title screens map banks 5 and 6). Banks 2-7 are
  readable at 0x10000 (nothing here needs them). Money is binary big-endian, not BCD; the party struct is 0x30 bytes
  with a held item and five stats and no types (the species' base data has them).
- **State** has the Gen 1 shape plus `item` and `stat_names` on party members, `time_of_day`, Johto and Kanto badges
  (`badges` is their total out of 16, `badge_bits` is `johto | kanto << 8`) and `map_id = group << 8 | number`. The bag
  flattens the items, key items, balls and TM/HM pockets. Known dumps get map names from the constants files; hacks get
  the ROM's landmark name plus `(map group:number)`.
- **Screens.** The dialog box is rows 12-17 and its ▼ sits on the bottom border (row 17, unlike Gen 1's 16). Tile ids
  decode as letters for map tiles and Pokémon pictures too, so `screen_text` only reads inside boxes (┌ ... ┘) and
  skips picture boxes. Menus drop presses shorter than about four frames, so `press` holds four.
- **Walk grid.** Collision is per 16x16 step square: four bytes per block (top-left, top-right, bottom-left, bottom-right
  of the block's step squares) looked up in the permission table (0 walkable, 1 water, 0xF wall, bit 0x10 talk). Water
  counts as walkable only while surfing or when the Fog Badge and a party member with Surf allow it. Ledges (0xA0-0xA7)
  are floor you stand on; a further step in the hop direction jumps two squares over the wall that follows. Side walls
  (0xB0-0xB7) block entering them in some directions. NPCs come from the object structs (coordinates are offset by 4)
  and warps from the ROM's event list. Warp carpets and doors trigger on a step in their direction: `walk_to` ends on the
  square, then a `walk` in that direction leaves the map. The edge connections to neighbouring maps are not in the grid.
- **Verified live** (Gold and Crystal): new game to the starter, the first wild battle, `state_text`, `walk_to` across
  Route 29 and 30 ledges (a left, a down and a down-left hop on Route 29, a down hop on Route 30, each one-way), `advance_text`
  stopping at yes/no prompts, in-game save and `save_complete`, a `.sav` imported into a fresh cartridge. Silver shares
  Gold's layout (booted and compared). Not verified live: side walls (none on those routes), diagonal-up hops, surfing,
  trainer battles, key item, ball and TM/HM pockets with content, badges.
- **Save:** bank 1, check value 99 at 0xA008, the data from 0xA009, its 16-bit little-endian sum, check value 127.
  Gold/Silver: data ends at 0xAD68, checksum 0xAD69, check value 0xAD6B. Crystal: data ends at 0xAB82, checksum 0xAD0D,
  check value 0xAD0F.
- **Default objective:** the Hall of Fame map (group 16, map 8, id 0x1008) after the Elite Four, as in Gen 1. Defeating
  Red at Mt. Silver is a custom objective.

## Play loop *(implemented, phase 4)*

`pokeagent play --rom PATH [--objective TEXT]` creates a run; `pokeagent resume RUN` continues one where it stopped.
Both take `--max-sessions`, `--model`, `--effort`, `--watch`/`--no-watch` (the Tk window with the frame, the last
action and the status panel; on by default), `--web [HOST:]PORT` (the same on a read-only web page) and
`--watch-speed` (a multiple of real time; uncapped by default).

- **Run directory** (`<data dir>/runs/<id>/`, mode 0700): `run.json` (objective, trusted check, ROM path and
  sha1, core and options; written once), `state.json` (session count, last summary and focus), `notes.md`,
  `play.log` (one line per message, tool call and lifecycle event), `recovery.state` (the game after the last
  action, rewritten atomically after every action), `checkpoints/` (the last 10 states with their notes and
  progress, written every 25 actions, at every session end and on stop), `sessions/NNNN.jsonl` (raw streams),
  `web.jsonl` (the web hook's log) and `.lock` (one pokeagent process per run). Harness issues go to
  `<data dir>/harness-issues.md`.
- **Objective:** only the adapter's default objective gets its trusted check (Red/Blue: the Hall of Fame map),
  evaluated before every session once a game is in progress (power-on RAM is noise that could match by chance).
  A custom objective has only the user's `--success` check, if any (see Objectives below).
- **`server.py`:** the `game` MCP server on a free 127.0.0.1 port, in a background thread. A per-session bearer
  token is checked before MCP sees a request, and the Host header must name this server. Tools: `get_state`
  (full text, whole-map ASCII, 3x screenshot; free), the actions `press`, `walk`, `walk_to` and `advance_text`
  (budget 100 per session; results report what changed, the screen text and a 9x9 map crop, with a screenshot
  only when the screen mode changed), `notes_read`/`notes_write` (8 KB), `end_session(summary, next_focus)` and
  `report_harness_issue`, plus the knowledge, objective and capability tools described below.
- **`play.py`:** `Game` serialises every emulator call under one lock and is what the adapter drives, so it can
  pace emulation and feed the viewer. `Runner` runs one session at a time with a brief (objective, check, budget,
  last summary and focus, notes, current state):
  - `limit`: waits until the reported reset (at least a minute, 15 minutes without a reset time);
  - `auth_error`: stops the run; `InitRejected` stops it before any model request;
  - `error`: backs off 30 s, 60 s, …; five in a row stop the run;
  - `completed`, `max_turns` and `timeout` continue with the next session.
  - If the emulator worker fails, a new one starts at `recovery.state` (or the latest checkpoint, or power-on) and
    the failed call reports that it had no effect, so at most that one action is lost. A failed `recovery.state` write (disk full) counts as an emulator failure, so the emulator rolls back to the last written state. After three restarts in one
    `play`, every game tool refuses with the reason and the run stops after the session.
- **Play style** (`--style`, fixed in `run.json`): `natural` (default) adds house rules to the brief (catch new
  species, a balanced team without shared types, even training, items, the PC, nicknames) with the run's variety,
  drawn once at creation by `draw_variety` (the starter the fewest earlier runs drew, three team types besides the
  starter's, a nickname theme, three of the adapter's side quests), and a line of team facts (level range, shared
  types, members without a nickname). `direct` adds nothing. `--glitches` adds the adapter's glitch allowance
  (`GLITCHES`); without it the prompt rules out glitches. Side quests and house rules are guidance, not checks.
- **`--from RUN`** starts a new run on that run's ROM from its latest state (copied as the new run's
  `recovery.state`; the core and options must match); `run.json` records it as `from`. **`--rom ROM --sav FILE`**
  starts the cartridge with that in-game save (any emulator's 32 KB `.sav`, checked with the game's checksum) and
  keeps the power-on state as the first `recovery.state`, so the agent chooses CONTINUE itself.
- **Resume** loads `recovery.state` (else the latest checkpoint, else power-on). Notes and progress are current with
  it, so nothing is replayed or restored.
- **State before a game:** until a game is started or loaded, the adapter reports only the screen, because WRAM
  holds SameBoy's randomised power-on contents.
- **Prompt:** `prompts/play.md` explains the tools, the budget, what persists and the rules (controller input
  only; web content is unverified). It names no capability to build.

## Milestones and the stuck ladder *(implemented, phase 7b)*

- **Milestones** are `milestone` events, so they belong to the timeline they happened on: the first visit to a map
  (on the current timeline's history), a new badge and a new Pokédex entry, read from the game state after every
  completed action. A new badge also takes a checkpoint. The brief lists the last five, and a restore lists the
  ones it undid.
- **Progress** is `progress_score` rising above its best: badges, maps visited on this timeline, Pokédex entries,
  levels, experience and tiles visited (since the runner started). Grinding on the same tiles is progress; pacing
  them or bumping into a wall is not. In a battle, the enemy's HP falling below its lowest so far in that battle
  (damage dealt, however it is dealt) and its next party member coming out count too; meeting a foe, healing back
  up, or both sides healing, does not. The ladder keeps that battle memory and drops it when the battle ends.
- **The ladder** (`stuck.py`, held by the runner): every 40 completed actions without progress climb a level, and a
  climb adds a notice (with knowledge hits for the current map) to that action's result and a `stuck` event:
  1. the notice only;
  2. the action tools refuse until `reflect(analysis, plan)` (a free play tool, recorded as a `reflection` event); the hold outlives the session, so a next session still starts held until it reflects;
  3. the session ends, and the next brief says how long the agent has been stuck and what comes next;
  4. the session ends and the runner restores the first checkpoint saved after the last progress (checkpoint
     events record the ladder instance and its best score, so checkpoints from before a resume never match) on a new timeline; with no such checkpoint it goes to level 5;
  5. the session ends and the run stops with `stuck`; `resume` continues it.

  Progress resets the ladder. It is not saved, so a resumed run starts it over.

## Metrics *(implemented, phase 7c)*

`stats.py` reads everything from events; nothing is counted twice or kept elsewhere.

- `pokeagent status RUN`: state, objective and checks, play and develop sessions with actions, tokens and cost, the
  timeline, the planned focus, the last five milestones, the last stuck notice, the active capabilities (newest
  first), recent knowledge and the last development cycle. The game window shows the same text in a panel
  under the frame, refreshed at session starts and ends, milestones and stuck notices.
- `pokeagent runs`: one line per run (state, play sessions and actions, and the map, badges and party read from its
  latest state on a fresh emulator, about 0.3 s per run).
- `pokeagent web [[HOST:]PORT]`: `viewer.browse`, a read-only run browser (`/`, `/run/<id>` with the status panel,
  latest frame and the newest 24 current-timeline checkpoints); see Decisions.
- `pokeagent view RUN [CHECKPOINT]`: the game window (and/or `--web` page) with the latest state (or a checkpoint of
  the current timeline) and the status panel, without playing. `--keyboard` plays a copy with the keyboard instead,
  two frames per step at the core's speed, and `--out` keeps an in-game save made there. `pokeagent export-sav RUN [CHECKPOINT] [--out PATH] [--force]`: the game
  saved in game as a standard `.sav` (see Decisions).
- `pokeagent stats RUN`: one row per session (mode, end, turns, actions, capability calls and failures, milestones,
  tokens, cache reads, cost, minutes), totals per mode and the development share of tokens. A session without its end event shows as
  `running` while a pokeagent process holds the run's lock, else as `interrupted`.
- `pokeagent stats RUN --compare RUN2`: for every milestone both runs reached (by its text, such as `badge 1` or
  `first visit to Pewter City`), the session, actions, tokens, cost and minutes each run needed to first reach it.
  Minutes leave out usage limit waits (`limit_wait` events). The CLI reports usage only at a session's end, so a
  milestone is charged its whole session's tokens and cost.

## Isolation and permissions *(sandbox, Claude provider and web hook implemented; the rest planned)*

- `sandbox.ensure()` runs the probe once per process and raises `SandboxError` unless all of these hold:
  - network is blocked;
  - a canary file outside the mounts is absent;
  - the package mount is read-only;
  - the rlimits are in force.

  Every `Emulator` calls it first.

- `agents/claude_cli.py` launches each `claude -p` session itself:
  - environment: scrubbed (`HOME`, `PATH`, `USER`, `LOGNAME`, `LANG`), with stdin from /dev/null;
  - tools: only pokeagent's MCP server plus WebSearch/WebFetch (`--tools`, `--strict-mcp-config`), under
    `--permission-mode dontAsk`, with no user or project settings (`--setting-sources ""`) and
    `--disable-slash-commands`;
  - `--settings`: disables the CLI's builtin AGENTS.md plugin and auto-memory, and installs the web hook;
  - MCP config: written per session with mode 0600 and removed afterwards; the token is never logged.
- Fail closed: every session's `init` event is checked (tools, MCP servers, builtin-only plugins without
  agents-md, no skills, slash commands or memory paths, `dontAsk`). Any deviation kills the session and raises
  `InitRejected`, which stops the run. A missing or malformed `init`, or any assistant/user/result event before a
  validated one, is rejected the same way; any other exception in the session loop kills and reaps the process group.
  The web hook blocks (exit 2) when its log cannot be written. `init` precedes the first model request, so a rejection
  costs no tokens.
- How a session ended, from structured fields first:
  - **completed:** a normal end;
  - **max_turns:** a normal end;
  - **limit:** a `rate_limit_event` whose status is not `allowed*`, or `api_error_status` 429; it carries
    `resetsAt`;
  - **auth_error:** 401/403;
  - **timeout:** the process group was killed at the wall-clock limit;
  - **error:** anything else.

  The limit path is tested with a synthetic stream only; the first real limit stream becomes a fixture.
- The MCP server binds to 127.0.0.1, checks host/origin, and maps each per-session bearer token to a mode. Play and
  develop sessions use two ports with separate tool lists, and a token works only on its own mode's port, so develop
  sessions have no route to the live emulator.
- Agent-written code runs only in bubblewrap: `--unshare-all --die-with-parent --new-session --clearenv`, minimal
  read-only mounts, a tmpfs, rlimits and a wall-clock timeout. A startup probe (network connect and a read of `~`
  must fail) fails closed.
- No memory-write path exists. RAM reads are limited to RAM regions (never ROM banks).
- The PreToolUse hook (`pokeagent hook web --log …`) logs every WebFetch/WebSearch and blocks:
  - every URL that is not `http(s)://` plus a plain ASCII host name or IPv4 address (letters, digits, dots,
    hyphens), an optional port and a printable-ASCII path. The fetcher parses URLs differently from Python
    (backslashes as slashes, percent-decoded and Unicode-normalized hosts, userinfo), so anything else in the
    authority is blocked rather than interpreted;
  - local or non-public hosts (private and loopback IPv4, `localhost`, dotless names, numeric last labels such as
    `127.1`, `.local`/`.lan`/`.internal`/…), compared without a trailing DNS dot;
  - token-like runs (24+ hex characters, or 32+ base64 characters mixing letters and digits, where a slash ends a
    run so a path of words is not one), also in the hostname and in percent-decoded text;
  - path plus query, or search text, over 200 characters.

  Malformed hook input is blocked too.

## Objectives, memory, timelines *(implemented, phase 5)*

- **Store:** `<data dir>/memory.db`, one SQLite database shared by every run so knowledge carries across runs. A run is
  recorded from its `run.json` the first time it is opened; afterwards the database decides and `run.json` must
  agree. Triggers enforce the rules for every writer:
  - `runs`: the objective and ROM never change; status is `running`, `claimed` or `complete`.
  - `checks`: condition, source and reason never change and no check is deleted. Sources are `adapter` (the default
    objective's trusted check), `user` (`--success`, or an approved proposal) and `agent`. An agent's check is never
    `active` (authoritative); only `proposed` moves to `approved` or `rejected`.
  - `events`: append-only (actions, session starts and ends with usage and cost, checkpoints with their core and ROM
    identity, restores, emulator restarts, claims).
  - `knowledge`: topic and content never change and nothing is deleted; status (`hypothesis`, `confirmed`,
    `refuted`, `stale`), evidence and `superseded_by` do. FTS5 indexes topic and content.
  - `timelines`: every run starts on one; a restore branches a new one from its checkpoint (parent and branch
    checkpoint) and abandons the current one. Events and knowledge record theirs.
- **Conditions:** `{"obs", "op", "value"}` over the adapter's observations, combined with `all`, `any` and `not`.
- **Judging,** before every session once a game is in progress: an active check that holds completes the run. The
  agent's latest proposed check that holds, or `report_objective_complete` without any active check, sets the run to
  `claimed`. A claimed or complete run does not resume. `pokeagent objective confirm` completes a claimed run;
  `reject --reason` resumes it, rejects the claim's check so it cannot pause the run again and tells the agent why
  in the next brief; `approve-check` makes the latest proposal a user check.
- **Knowledge tools:** `knowledge_add`, `knowledge_search`, `knowledge_set_status`. Scope is `run`, `rom` (sha1),
  `family` or `general`; a search sees its own run, its ROM, its family and general entries, without refuted or
  superseded ones. Entries are hypotheses; one is confirmed only with evidence, an `action` event of this run's
  current timeline (every action result starts with `[event N]`). An entry with a url (from the web) starts as a
  hypothesis whatever the evidence. `progress` entries are always run-scoped.
- **Brief:** objective; each check with its source and whether it holds now; the latest proposal; the observations
  with their values; a rejected claim; the last summary (or one generated from the session's last actions when it
  ended without `end_session`); notes; the top 5 knowledge entries matching the current map and the planned focus;
  where recent play went (the last five play sessions' actions, milestones and cost, and the last one's most repeated
  actions and three-action sequences, as numbers without advice); a restore since the last session; the current state.
- **Restore:** `pokeagent restore RUN` lists the checkpoints of the current timeline's history (its own, and its
  ancestors' up to where it branched); `pokeagent restore RUN CHECKPOINT` takes a stopped run back to one:
  - refused when the checkpoint was saved with another ROM, core or core options than the run's;
  - a new timeline starts at the checkpoint; the abandoned part (later checkpoints and actions) is out of reach, so
    its actions are no evidence and its checkpoints cannot be restored;
  - progress knowledge recorded after the checkpoint becomes `stale`; progress entries changed after it go back to
    unsuperseded hypotheses; durable knowledge (every other kind) and later capabilities stay;
  - the game (`recovery.state`), notes and planned focus become the checkpoint's; the session count stays;
  - the next brief says when the checkpoint was saved, how many actions were undone, which entries went stale and
    which note lines were dropped.

## Capabilities *(declarative, phase 6a; code extensions with contracts and validation, phase 6b; develop mode, phase 7a)*

- **Builtin:** `get_state`, `press`, `walk_to(x, y)` (whole-map grid), `walk`, `advance_text` (stops at prompts).
- **Declarative** (`capabilities.py`): plain data that trusted code validates and interprets; no agent code runs.
  - *Observation:* a name, a description, an address, a length (1-64) and a decode (`u8`, `u16le`, `u16be`, `bcd`,
    `bit` with a bit number, `popcount`, `bytes` or `text`). `define_observation` reads it once (and refuses it
    when the optional `expect`, such as `{"op": "==", "value": 5}`, fails), then `get_state`, action results' state
    text and the brief show it when `show` is set. Reads go through the worker's RAM-only `read`, so ROM and
    unmapped addresses are refused like `mem_read`'s.
  - *Macro:* up to 20 steps over `press`, `walk`, `walk_to`, `advance_text` and other macros, each with optional
    `repeat`, or `until`/`while` (conditions in the success-check grammar over builtin and agent observations) with
    a required `max`. `params` is a JSON schema for `run_macro`'s args, referenced in steps as `{"$arg": "x"}`.
    Steps and conditions are checked when the macro is defined; it is not run then.
  - *Calls:* `run_macro` is one action of the session budget; inside it a call runs at most 200 builtin actions, 5000 loop iterations and nested calls in total, and
    4 nested macros. Arguments that do not match `params` are refused and not counted; a step's error or an `until`
    that never held fails the call, which still reports the state it left behind.
  - *Versions and scope:* every definition is a new immutable version (`capabilities` table, triggers) scoped to
    the ROM (`rom:<sha1>`) or the family (`family:pokemon-gen1`); later runs on that ROM or family see the active
    version, a ROM one hiding a family one of the same name. Capabilities are durable: a restore keeps them.
  - *Rollback:* every call is a `capability_call` event; 3 failures in a version's last 10 calls retire it and
    reactivate the version before it (or none). `pokeagent capability rollback RUN NAME` does the same by hand. The
    next brief says what was rolled back and lists the active capabilities with their calls and failures.
  - *Free tools:* `mem_read(address, length)` (hex, RAM only), `define_observation`, `define_macro` and
    `list_capabilities`.
  - *Live check (2026-11-27, one 100-action session continuing run 1a on Route 2):* unprompted by name, the agent
    defined 7 macros (two redefined as version 2) that nest up to three deep, among them `grind_one` (pace in the
    grass until a battle, then fight until it ends: one action per wild battle) and a Pokémon Center round trip, and
    called them 48 times without a failure. Bulbasaur went from L7 to L13 for $1.63. Its two tool errors were its
    own: defining a macro before the one it calls, and acting after the budget ran out. It used `mem_read` but
    defined no observation.
- **Code extensions** (`extensions.py`, `extension_host.py`): a manifest plus `tool.py` with `run(ctx, **params)`,
  the `tool` kind in the same registry, versions and rollback.
  - *Manifest:* name, description, `params` and `returns` (JSON schemas), `limitations`, `compatibility` (family,
    optional ROM sha1s), `requires`, `preconditions`, `invariants` and `postconditions` (conditions; only
    postconditions may compare with `{"before": obs}`), `effects` (fingerprint groups: position, map, party, bag,
    money, battle, menu), `prefer_over` (a builtin action the brief says to use it instead of; the builtin stays),
    `examples` (1-5 argument sets for the validation runs; default `{}`) and `api_version` 1.
  - *Storage:* `<data dir>/library/<sha256>/tool.py`, content-addressed, so a version's code never changes.
  - *Host:* each call spawns `extension_host` in bubblewrap with only that folder mounted at `/ext` (read-only), no
    ROM, no network, no home, a tmpfs `/tmp` and rlimits (1 GiB address space, 16 processes, 120 s CPU). It talks
    JSON lines over its pipes; the orchestrator treats every line as untrusted (size cap, method and argument
    checks) and kills the process group when the call ends.
  - *`ctx` (API 1):* `state()`, `observe(name)`, `read(address, length)` (RAM only, at most 64 bytes),
    `screen_text()`, `press(button, times)`, `hold(buttons, frames)`, `wait(frames)`, `call(name, **args)` (a
    builtin action, a macro or another tool), `log(message)`, `kv` (durable per tool and scope, in the memory store)
    and `tkv` (`tkv.json` in the run directory, saved with every checkpoint and every action's recovery snapshot, and put back by a restore or an emulator restart). Each store
    holds at most 64 KB of JSON.
  - *Contract, enforced by the orchestrator:* arguments against `params` and the `preconditions` before the call (a
    refusal does not count against the tool); during it one budget shared by every nested call (18,000 frames, 600
    controller inputs counted as the game executes them, 60 s), at most 4 nested calls, and the `invariants` after
    every request that ran the game; after it the result against `returns`, the `postconditions`, and every changed
    fingerprint group must be a declared effect. Undeclared party, bag or money changes therefore fail the call. A
    refused ctx request (a bad argument, a failed nested call) is an error the tool may catch; a budget, depth,
    invariant or protocol violation ends the call. Failures count like failed macro calls and roll a version back.
  - *Play:* `run_tool(name, args)` is one action; the result, the tool's log and the state are reported like a
    macro's.
  - *Validation* (`validation.py`, orchestrator-owned; the agent can ask for one but not change it): the manifest;
    ruff's syntax and undefined-name checks on `tool.py` and `test_tool.py` (ruff only reads them); the agent's
    tests (`test_*(ctx)`, which may `ctx.load_fixture(name)`) in the sandbox; then trusted contract runs of every
    example on up to three states (the live state first, then fixtures and earlier checkpoints) where the
    preconditions hold. At least one state must run and none may fail, so the agent's tests alone never pass a
    tool. It runs on its own emulator with scratch `kv` and `tkv`, so it changes nothing the run keeps. The
    report is a `validation` event.
  - *Activation:* a tool that passes is staged (a newer validation replaces a staged version); at the next session
    start the runner takes a checkpoint, activates every staged version and the brief says so.
    `extensions.install` (check, store, activate at once) remains for trusted callers and tests.
- **Develop mode** (`develop.py`, the develop endpoint of `server.py`, `Runner._develop`, `prompts/develop.md`):
  - *Request:* in play, `request_development(tool, problem, evidence, expected_benefit, proposal)` names a new or
    existing tool and cites 1-10 action events of the run; one request per session, recorded as a
    `development_requested` event.
  - *Policy, after that play session:* the first development cycle of a run always runs. After it, a request is
    refused when its tool already had `--dev-attempts` cycles (2) or develop sessions have used more than
    `--dev-share` (0.3) of the run's tokens (input + cache creation + output from each session's reported usage;
    cache reads are excluded). Usage arrives only at a session's end, so the share is checked between sessions. A
    refusal is a `development_decision` event that the next play brief explains.
  - *Session:* `claude -p` with the develop prompt against the develop port, up to 200 controller actions, 300 turns
    and `--dev-timeout` (45 min). Its `Bench` drives a second emulator started from a copy of the live state; `dev_load`
    restarts it there or at a checkpoint of the current timeline. Its actions are not game events and write no
    recovery state, so the live game, the run's timeline and its checkpoints stay as they were.
  - *Tools:* the controller tools on the development game; `mem_read`, `mem_snapshot` (work RAM, at most 16),
    `mem_diff` and `mem_search` (read-only); `fixture_save`/`fixture_load` (at most 20 per tool, 1 MB each); `ws_list`,
    `ws_read`, `ws_write` and `ws_delete` in `run_dir/workspace/<tool>/` (relative paths only, no `..`, no links,
    at most 200 files and 5 MB); `library_read` (an active tool's manifest and code); `validate` (at most
    `--dev-validations`, 10); and the notes, knowledge, `list_capabilities`, `report_harness_issue` and
    `end_session` tools both modes share, plus WebSearch/WebFetch.
  - *Validation:* the workspace's manifest must name the requested tool. The states are the live state, the tool's
    fixtures and the last two checkpoints, each run on a fresh emulator. A pass stages the version; it becomes
    active at the next play session.
  - *Live check (2026-11-27):* three 100-action play sessions continued the phase 6a live copy from Pewter City
    ($4.46). The agent beat Brock, crossed Mt. Moon and beat Misty, defined 7 more macros (16 versions in all) and
    made 38 macro calls with 3 failures, but did not request development. A develop session was then started on a
    scratch copy with a harness request that names no gameplay capability (so it is no evidence for A13). It took 10
    turns ($0.14): it wrote the three files, saved a fixture and validated twice. The first validation failed because
    its tests imported `tool.py`; it switched to `ctx.call` and the second passed on the live state, the fixture and
    a checkpoint, and staged the tool. The develop prompt now says that tests call the tool through `ctx.call`.
  - *Records:* `session_start`/`session_end` with `"mode": "develop"` (the end carries the usage the policy counts)
    and a `development_cycle` event (request, tool, attempt, summary, validations, staged version) that the next play
    brief reports. A develop session counts toward `--max-sessions`. A request left unhandled when a run stops (the session limit, a crash) is recovered from its event on the next invocation.

## Verified facts

From throwaway spikes on 2026-11-27 (Fedora 44 x86_64, bwrap 0.12.0, Claude CLI 2.1.326, libretro.py 0.12.0).

**Emulator (libretro.py + SameBoy 1.0.2 `be2c352` from RetroArch 1.22.2 stable)**

- 59.7275 fps, 160×144. Frames arrive as XRGB8888; Pillow decodes them with raw mode `BGRA`.
- Headless speed is about 175 emulated fps, inside bwrap as well.
- With the default `greyscale` palette, the dmg-acid2 frame matches the reference PNG pixel for pixel. Colours are
  0, 85, 170 and 255.
- `serialize_size` is 252650 bytes. Replaying the same inputs from a restored state gives identical WRAM hashes and
  identical serialized states.
- `get_memory(SYSTEM_RAM)` returns the 8 KB WRAM at C000–DFFF. The core's memory map also exposes:
  - HRAM (FF80, 128 B), IE (FFFF), IO (FF00) and OAM (FE00);
  - cartridge RAM (A000, 8 KB) and VRAM (8000, 8 KB);
  - ROM (0000 and 4000, flagged const).

  `retro_memory_descriptor.view()` reads any of them.
- A second `Session` in the same process segfaults (exit 139), so each core gets its own process.
- **Core output:** the core prints to the process's stdout and stderr itself, even with `log=None`. The worker
  therefore keeps RPC off fd 1.
- **Boot ROM:** without `dmg_boot.bin` in the system directory, SameBoy logs a missing-boot-ROM line and uses its
  built-in boot ROM.
- **Options:** `sameboy_model` (Auto, Game Boy, GBC revisions…), `sameboy_mono_palette`, `sameboy_rtc` and others,
  all readable through `session.options`.
- SameBoy refuses to serialize before the first `retro_run`, so the worker runs one frame at startup.
- SameBoy outputs audio at 2,097,152 Hz. libretro.py's default `ArrayAudioDriver` keeps every sample (about 8 MB per
  emulated second), which took the first live session's worker to its 2 GB address-space limit after about four
  emulated minutes; it then stopped answering. The worker discards audio.
- A serialized state carries the whole 32 KB cartridge RAM. After an in-game save, loading the earlier state also
  restores the earlier save data, so checkpoints need no separate `.sav` and an in-game save cannot leak between
  timelines.
- The same 32 KB (libretro's `RETRO_MEMORY_SAVE_RAM`, all four banks) is what a standard Red/Blue `.sav` holds. After
  an in-game save the player name sits at file offset 0x2598 and the main data checksum (`~sum` of 0x2598-0x3522) at
  0x3523, as in pret's `sPlayerName` and `sMainDataCheckSum`; a test saves in game and checks both. An exported save
  loaded in PyBoy offered CONTINUE with the same player, badges, map and position.

**Pokémon Red/Blue (English)**

- The pret pokered symbols used by the adapter are identical in the Red and Blue builds.
- Yellow (sha1 `cc7d03262ebfaf2f06772c1a480c7d9d5f4a38e1`, MBC5, CGB-flagged) boots headless on SameBoy with
  `sameboy_model` "Game Boy" and "Game Boy Color"; the screen tile map and charmap match Red's. WRAM matches Red before
  `WALK_COUNTER` and is one byte lower from there on (pokeyellow.sym). ROM tables: species 3A:4000, items
  01:45B7, moves 2F:4000, types 09:7D63, tile pairs 00:0ADA, ledges 06:6851. Preset names are YELLOW/ASH/JACK and
  BLUE/GARY/JOHN; the Oak event in Pallet Town fires one step further north and starts a scripted wild Pikachu
  screen. Not verified: receiving Pikachu and the follower on a walked grid.
- Blue Kaizo differs from Blue only in gameplay data (trainers, wild Pokémon, stats, maps); names, tables and RAM
  addresses are unchanged, so it works with the Red/Blue layout. Japanese Red/Green/Blue are not supported: pret has no
  Japanese target (the one fork is a 2017 work in progress), and they need ROMs to test, a kana charmap and shorter
  name records.
- After a warp, the map id changes about 30 frames before the new map's blocks are loaded; reading the walk grid
  earlier shows the old map. `step` waits until the map buffer changes and input is accepted again.
- Menus drop a direction press made in the first frames after they open; `choose` re-reads the cursor and retries.
- Oak's fades and screen transitions run longer than the gaps between text boxes, so `advance_text` treats text as
  closed only after 60 quiet frames.
- Map events can start a few frames after a step ends (Oak's event in Pallet Town), so a walk can report `arrived`
  with a dialog appearing right after it; tool results show the screen after a short settle.
- Red and Blue swap the preset names (RED/BLUE versus BLUE/RED).
- Looking at a starter's ball in Oak's Lab sets four Pokédex owned flags while its entry is shown, before the player
  has a Pokémon. The adapter reports nothing owned while the party is empty (live run 2 recorded a false
  "4 species owned" milestone before this).
- A menu or prompt that accepted a press keeps its box and cursor on screen for about 16 frames. `press` therefore
  waits until the screen changes (up to 32 frames); with a fixed 12-frame wait, live run 1a saw stale menus and
  pressed again, believing its input had been dropped. Hold length does not matter: 1 to 6 frames are all accepted.

**Live run 1a (2026-11-27, Red, default objective, 3 sessions of 100 actions)**

- About 3 minutes, 110 to 130 turns and about $1 per session (CLI-reported cost).
- From power-on to the Pewter City Pokémon Center: starter (Bulbasaur), the rival battle, Oak's Parcel and the
  Pokédex, Poké Balls, two catches, Viridian Forest and its Bug Catcher, Bulbasaur at level 7. No badge yet.
  Verified from the last checkpoint, not from the agent's summary.
- Harness problems found and fixed: the worker's audio buffer filled its memory limit (first attempt), power-on RAM
  crashed the state reader (second attempt), and stale menus after a press (agent notes). The only tool errors in the
  run were calls after the action budget was used up.

**Live run 1 (2026-11-27, phase 8: run 1a resumed for 37 more sessions, the 40-session experiment)**

- Sessions 4 to 40 played without intervention: 3,655 actions in 181 minutes of sessions, 3.1 million tokens
  (cache reads excluded) and $62.15, about $1.70 and 5 minutes per session. No usage limit wait, emulator restart or
  harness issue occurred.
- From the Pewter City Pokémon Center to Silph Co. 9F: five badges (sessions 5, 8, 18, 25 and 35), 107 maps and 14
  species owned, read from milestone events. By the agent's summaries, consistent with the maps it reached, it also
  got the S.S. Ticket, Cut, Flash, the Silph Scope and the Poké Flute.
- Declarative self-improvement (A12) happened unprompted. The agent defined 32 macros and 2 RAM observations
  (`battle_my_hp`, `enemy_species`), five of the macros in a second version after it saw them misbehave. It called
  29 of them 437 times, and one call failed. They covered battle turns, trainer intros, healing round trips, grinding,
  fishing and a Rocket Hideout spinner route, and the observations let a stall macro stop when its HP got low.
  Its 36 knowledge entries are routes, lessons, mechanics and technical facts, all confirmed by it.
- Programmatic self-improvement (A13) did not happen: no session called `request_development`. Battles, routes and
  menus were all within reach of macros with conditions, and nothing in the logs shows the agent meeting a
  problem it said they could not handle.
- The stuck ladder reached level 1 six times (shopping, long dialogs and a stall battle) and never level 2. One
  `reflect` call came unasked at level 1, noting that damage dealt in a long battle does not count as progress.
- Tool errors were almost all action calls after the budget ran out (42 of 56), plus definition mistakes the
  validator caught. The web hook blocked three fetches of pret disassembly files as token-like: the slashes of the
  path made one long base64 run. A slash now ends a run.

**Live run 2 (2026-11-27, phase 8: Red from power-on, 20 sessions, same data dir as run 1)**

- 1,921 actions, 1.5 million tokens and $29.04 in 20 sessions, without intervention, usage limit waits or restores.
  It reached Route 9 with three badges and seven species owned. Its briefs carried the efficiency lines (PR 28).
- Reuse (A14): run 1's capabilities were active from the first session. 316 of run 2's 350 capability calls went to
  19 of run 1's macros, none failed, and the agent added 11 definitions of its own. Its knowledge searches returned
  15 of run 1's 33 ROM-scoped entries (routes, the S.S. Anne, Rock Tunnel, macro lessons).
- `stats --compare` lines up the runs by milestone. Run 1's first three sessions (299 actions, $3.31) predate the
  event store, so its totals below include them by hand; its first milestones before Pewter City are not comparable.

  | milestone | run 2: session, actions, cost | run 1: session, actions, cost |
  |---|---|---|
  | badge 1 | 3, 253, $3.64 | 5, 406, $5.64 |
  | badge 2 | 6, 534, $8.00 | 8, 752, $10.38 |
  | badge 3 | 18, 1,684, $25.87 | 18, 1,734, $24.31 |

  Run 2 reached the first two badges with about 30% fewer actions. Between Cerulean and Vermilion it lost the
  difference: with a weaker team it blacked out on Route 25 and Route 6 and took two sessions to recover.
- No session requested development (A13), now with the efficiency lines in the brief: 57 sessions with the tools
  across both runs, no request (#26).
- Its first "4 species owned" milestone was false: looking at a starter's ball sets four owned flags (fixed in
  PR 29, after the run started).

**Sandbox (bwrap)**

- libretro runs unchanged under `bwrap --unshare-all --die-with-parent --new-session --clearenv` with:
  - read-only `/usr`, the uv Python, the venv and the package;
  - core and ROM bound read-only;
  - fresh `/proc`, `/dev` and a tmpfs `/tmp`.
- Inside the sandbox:
  - TCP connect fails (ENETUNREACH);
  - `/etc`, `/var`, `/run/user` and the rest of `~` are absent;
  - writes to `/usr` fail (EROFS).
- Rlimits:
  - `RLIMIT_NPROC` set *before* bwrap counts all of the user's processes and stops bwrap from creating
    namespaces, so a trusted entry point sets every rlimit inside the sandbox before running anything;
  - inside the user namespace NPROC counts only the sandbox's processes;
  - hard limits cannot be raised again.

**Claude CLI (`claude -p`, claude.ai login)**

- Environment: the CLI starts under a scrubbed environment (`HOME`, `PATH`, `USER`, `LANG`), which drops the
  `CLAUDE_*`/`ANTHROPIC_*` variables a parent agent may export. `claude auth status --json` reports `loggedIn` and
  `authMethod` (`claude.ai`).
- With `--setting-sources ""`, the debug log shows:
  - the user's installed plugin is disabled;
  - 0 hooks are registered;
  - 0 user skills or slash commands load;
  - no CLAUDE.md or user memory content is in context (the model confirmed).
- The CLI's own builtin plugins still load. One of them, `cc-plugin-agents-md`, loads AGENTS.md files. Auto-memory
  is also on.
- `--settings` with `{"enabledPlugins": {"cc-plugin-agents-md@builtin": false}, "autoMemoryEnabled": false}`
  removes both: `init` lists no such plugin and `memory_paths` is null.
- `--tools "WebSearch,WebFetch"` plus `--mcp-config` and `--strict-mcp-config` yields exactly:
  - `tools = [WebFetch, WebSearch, mcp__game__…]`;
  - `mcp_servers = [{name: game, status: connected}]`;
  - `slash_commands = []` and `skills = []`.
- MCP streamable HTTP on 127.0.0.1 with a `headers.Authorization: Bearer …` entry works. Tool results with an
  image reach the model (it described the acid2 face correctly), and tools read headers through `Context.headers`.
- A PreToolUse hook from `--settings` runs under `--setting-sources ""`. It receives `tool_name` and `tool_input.url`
  (or `.query`). Exit code 2 blocks the call and the block is listed in `result.permission_denials`.
- Turn limit: `--max-turns N` ends with `subtype error_max_turns`, `terminal_reason max_turns` and exit code 1.
- Every stream has these events:
  - **`init`:** tools, mcp_servers, plugins, slash_commands, skills, memory_paths, permissionMode, model;
  - **`rate_limit_event`:** `rate_limit_info.status`, `resetsAt`, `rateLimitType`, and `unifiedWindows` with
    utilisation per five-hour and seven-day window;
  - **`result`:** `usage` (input, cache creation, cache read, output, server tool use), `total_cost_usd`,
    `modelUsage`, `num_turns`, `terminal_reason`, `api_error_status`.
- Without input on stdin the CLI waits 3 s; the provider passes `stdin=DEVNULL`.

**GBA (mGBA 0.11-dev `c758314` from the same archive; spikes 2026-11-28)**

- `mgba_libretro.so` sha256 `564c32a7214fe52f7d3b7ead5fbe70a7e45b5ccb3b52da2c07bc1b6149de75a5`. 240×160, 59.7275 fps,
  serialized state 528448 bytes, about 1500 emulated fps in the sandbox. Defaults need no options and no BIOS file.
- The memory map (11 descriptors) exists only after the first `retro_run`: IWRAM 0x03000000 (32 KiB), EWRAM 0x02000000
  (256 KiB), save flash 0x0E000000 (128 KiB, readable), IO, palette, VRAM, OAM, BIOS and ROM (const).
  `get_memory(SYSTEM_RAM)` is EWRAM's first 32 KiB, not IWRAM.
- `save_ram` is a standard 128 KiB flash image: after one in-game save, 14 sectors of 4 KiB with the 0x08012025
  signature, ids 0-13 and checksums (data 3968 bytes + footer: id, checksum, signature, counter). It loads back as a
  battery file, and the game offers CONTINUE.
- Pret symbols: Ruby rev 1 = Sapphire (all revisions), FireRed = LeafGreen (both revisions) in RAM; only ROM addresses
  differ. The FireRed and LeafGreen save blocks are at different addresses in one run (LeafGreen's differ by 0x48), so
  the pointers are always dereferenced.
- Text: Emerald and FireRed/LeafGreen print through `sTextPrinters`; Ruby and Sapphire through a message window struct.
  `gMain.inBattle` is bit 1 of a byte at gMain+0x439 (Emerald, FireRed/LeafGreen) or +0x43D (Ruby, Sapphire).
- Radical Red (checked by hand, not supported): FireRed's save block pointers and party count addresses, `encryptionKey`
  0, unencrypted party Pokémon in block order GAEM with a zero checksum, species names at 0x1719ba0 with 11-byte Title
  Case entries, 17-byte move names. ROWE: save block pointers at 0x03004b80 (block 2) and 0x03004b84 (block 1).

## Decisions

- **2026-11-27: Unchanged upstream libretro cores through libretro.py, no emulator fork.** One Python API across
  GB/GBC (SameBoy), GBA (mGBA) and DS (melonDS DS); best-in-class accuracy without maintaining C code. Fork a single
  core only if it lacks a needed feature.
- **2026-11-27: The local Claude CLI (`claude -p`) is the first agent provider,** so runs use the user's own
  login without a Python SDK dependency. Other providers plug in behind `AgentProvider`.
- **2026-11-27: Python 3.13 via uv.** uv's managed CPython includes Tk (for the viewer) and SQLite FTS5.
- **2026-11-27: Tools over MCP streamable HTTP on 127.0.0.1,** served by the long-lived `pokeagent play` process, so
  the emulator, viewer and learned data survive between Claude sessions.
- **2026-11-27: Cores come from RetroArch's pinned stable `RetroArch_cores.7z`, not from a source build.**
  - Source: `buildbot.libretro.com/stable/1.22.2/linux/x86_64/RetroArch_cores.7z`.
  - Pins: the archive's sha256 (274 MB) and each extracted `.so`'s sha256 are pinned.
  - Why not a build: building SameBoy needs `rgbds` and a C toolchain, and the per-core "latest" nightlies are not
    pinnable.
  - Download: once per machine into the core cache, and the archive is deleted after extraction.
- **2026-11-27: Claude sessions run under a scrubbed environment.**
  - `--settings` disables the CLI's builtin `cc-plugin-agents-md` plugin and auto-memory.
  - `init` checks require every listed plugin to be builtin (`path: builtin`), with agents-md absent and
    `memory_paths` null.
  - `--bare` stays unused because it requires an API key instead of the claude.ai login.
- **2026-11-27: Sandbox rlimits are applied inside the namespace** by a trusted entry point, because a pre-bwrap
  `RLIMIT_NPROC` counts the user's whole process tree.
- **2026-11-27: No separate battery save files.** SameBoy's serialized state includes the cartridge RAM, so a
  checkpoint restores the in-game save with everything else and saves cannot cross timelines.
- **2026-11-28: `.sav` files only on request, saved in game.** `pokeagent export-sav` loads a copy of the latest state
  (or a checkpoint) in a fresh emulator, closes text, backs out of menus with B (never answering a yes/no prompt) and
  saves through the START menu, then writes the cartridge RAM once the game's own checksum holds. Copying the RAM
  without saving would export whatever the agent last saved, possibly nothing. The run is never changed, and an
  existing file is replaced only with `--force`. The worker's `save_ram` op is read-only like `read`.
- **2026-11-28: The game window is on by default.** A run is meant to be watched; `--no-watch` is for tests and
  headless machines. `pokeagent view` shows a stopped run's game and status without playing it.
- **2026-11-28: The web page is read-only and local by default.** `viewer.serve` (stdlib `ThreadingHTTPServer`)
  answers GET for the page, `/frame.png` and `/status.json` only, from the same object the Tk window shows. It binds
  127.0.0.1 unless `--web` names another host, and refuses requests whose Host header is a DNS name rather than an
  address or `localhost` (DNS rebinding). There is no login: on the network, anyone who can reach the port sees the
  game and the run panel, so that stays an explicit choice, and the firewall (here the Wi-Fi interface's `drop` zone)
  has to admit the port.
- **2026-11-28: `pokeagent web` draws frames on demand and stores none.** Run ids and checkpoint names come from the
  run listing (never a request path joined onto the disk), the same Host check, GET-only and no-store headers as
  `viewer.serve` apply, and nothing on it changes a run. A frame is a checkpoint's existing `.state` loaded on a fresh
  sandboxed emulator for one frame, so previous images cost no disk; a bounded in-memory cache (64 PNGs, keyed by run,
  file and mtime) keeps refreshing pages from booting an emulator per request, and the gallery stops at 24 checkpoints.
  The Tk window and both pages scale the 160x144 frame to the available space (nearest neighbour, aspect kept).
- **2026-11-28: Keyboard play is on a copy, without sound or `.sav` loading.** `view --keyboard` feeds held keys to
  a fresh sandboxed emulator loaded from the run's state, so the run, its timeline and the agent's knowledge never
  see human input. Sound would mean carrying audio out of the sandbox and a playback dependency, which is what
  other emulators are for; `--out` or `export-sav` hands the game over to them.
- **2026-11-27: Game names come from the user's ROM at runtime,** not from tables in the repository, and the walk grid
  is computed from the map and tileset data in RAM and ROM instead of hand-written maps.
- **2026-11-27: Timeline-bound context is saved with each checkpoint.** `notes.md` and `state.json` are copied next to
  the `.state` file, and a restore brings them back with the game, so a restored timeline never briefs the agent with
  progress it lost. Run directories are resolved to absolute paths because the Claude child runs with the run
  directory as its cwd.
- **2026-11-27: One SQLite memory store per data directory, with the rules in triggers.** Runs share it so knowledge
  found in one run is there for the next. Immutability (objective, checks, events, knowledge text) is enforced by the
  database for every writer, so a bug in pokeagent cannot quietly rewrite a run's objective or history either.
- **2026-11-27: Knowledge is confirmed only by evidence from the run's own game,** an action event of its current
  timeline. Web pages and the agent's own reasoning produce hypotheses; the brief and searches show the status.
- **2026-11-27: A replacement knowledge entry must be at least as widely scoped as the entry it supersedes,** or it
  would hide a shared entry from runs that cannot see the replacement.
- **2026-11-27 (Wout): Crash recovery uses a recovery state after every action instead of an input log and replay.**
  A state is about 250 KB and takes milliseconds, so resume and emulator restarts lose at most the interrupted action.
  Nothing is ever replayed, so an abandoned timeline cannot be replayed by accident, and there are no RAM hashes,
  input log or replay command to keep correct.
- **2026-11-27 (Wout): No goal tools.** In live run 1a the agent kept its plan in its notes and `next_focus`, which
  already travel with checkpoints. Milestones will be derived from game state for stats and stuck detection
  (phase 7) instead of being declared by the agent.
- **2026-11-27: Restores go back only along the current timeline's history.** Restoring into an abandoned branch
  would need to bring back progress knowledge that an earlier restore made stale; it is not needed by the stuck
  ladder, which restores the last checkpoint that made progress.
- **2026-11-27: Declarative macros are checked statically, not dry-run, when defined.** Their effect depends on the
  state they run in, so a dry run on a copy of the current state would mostly repeat the first real call. Calls are
  bounded (200 actions, 4 nested macros, a `max` on every loop) and a version that keeps failing in play is rolled
  back. Code extensions will still be validated on several states in the isolated worker (phase 6b).
- **2026-11-27: Success checks accept only the adapter's builtin observations,** not agent-defined ones, so an
  observation the agent wrote cannot decide that its own objective is met.
- **2026-11-27: A refused emulator request is a `RequestRefused` (a `ValueError`),** not a dead worker, so a bad
  address from `mem_read` or an observation is a tool error and never triggers an emulator restart.
- **2026-11-27: Code extension manifests are checked by the same plain validation as declarative capabilities,** not
  pydantic: one style for every definition the agent writes, and jsonschema already checks the schemas inside them.
- **2026-11-27: The extension library is content-addressed** (`library/<sha256>/`), so the registry row's hash
  names exactly the code that runs, and identical versions share a folder.
- **2026-11-27: Only top-level tool and macro calls count toward rollback.** A failed nested call is an error the
  caller sees and may handle; the caller's own outcome is what is recorded.
- **2026-11-27: No separate soft-lock invariant for tools yet.** Declared effects already fail a call that leaves a
  menu or dialog open; a game that stops responding is left to the stuck ladder (phase 7), which sees repeated
  fingerprints whatever caused them.
- **2026-11-27: ruff is a runtime dependency,** because validation lints agent code wherever pokeagent runs; it
  only reads the files, so it runs outside the sandbox.
- **2026-11-27: Develop sessions get their own MCP endpoint** rather than mode checks inside one tool list: each
  session's tool list is exactly its mode's, so a play session never sees workspace tools and a develop session's
  controller tools cannot be pointed at the live game. The two lists share one server object and its memory.
- **2026-11-27: Development attempts are counted per tool name,** the request's only stable key; a second request
  for the same tool continues its workspace. A cycle cut short by a usage limit or an error still counts.
- **2026-11-27: The stuck ladder climbs every 40 actions without progress.** Shopping, healing and long dialogs
  take tens of actions without progress, so 40 leaves room for them before the first notice, and the pause comes
  after 200 actions (two sessions). Live runs will tune it.
- **2026-11-27: The ladder's tile set and level live in the runner, not the run.** A resumed run starts the ladder
  over, which also lets the user resume a run that paused as stuck. Maps visited come from milestone events, so they
  follow the timeline.
- **2026-11-27: A tool's manifest carries its validation `examples`.** The contract runs need arguments, and the
  author of the tool is the one who knows sensible ones; each is checked against `params` first.
- **2026-11-27: Review fixes for tools.** A macro a tool calls spends the tool's shared budget action by action; a
  closed protocol pipe waits only until the call's deadline; the trusted contract runs start from fresh copies of
  the real stores, so the agent's own tests cannot decide whether they pass.
- **2026-11-27: Review fixes for tools, round 2.** A call refuses a tool whose `compatibility.roms` excludes the
  current ROM (a family-scoped tool is visible to every ROM in the family); a nested request that fails after
  changing the game still gets the caller's invariants checked; every action's recovery snapshot also holds the
  timeline store (`recovery.tkv.json`), so an emulator restart undoes a failed action's `ctx.tkv` writes too.
- **2026-11-28: Natural play is the default style, and its variety is drawn by code.** Wout wants runs to play
  like a person and differ from each other. The model alone tends to repeat its choices (the same starter and
  team), so pokeagent draws the starter, team types, nickname theme and side quests once per run and keeps them in
  `run.json`; the house rules stay guidance in the brief, because judging a "balanced" or "natural" game has no
  trustworthy check. `--style direct` keeps the shortest route for benchmarks. The four runs made before styles
  existed were migrated once to `"style": "direct"` with no variety, as they were played.
- **2026-11-28: Gen 1 versions come from scanning the ROM.** Yellow differs from Red/Blue in WRAM by one byte and
  in table addresses; hacks such as Blue Kaizo keep Red/Blue's. Rather than one sha1 and one address set per dump, the
  adapter scans for its ROM tables at construction (`recognises` = all found) and keeps two WRAM layouts, picked from
  the header and confirmed by a read-only probe of the running game. Ruled out: subclasses per version (the games
  differ in data, not behaviour) and a sha1 allowlist (it refuses every hack).
- **2026-11-28: Glitches are opt-in and stay controller input.** `--glitches` only changes what the agent may do
  with the controller; there is still no memory-write path. Glitch routes can satisfy the Hall of Fame check, which
  is accepted as for a glitched speedrun.
- **2026-11-28: Objective ideas from the agent are suggestions only.** `suggest_objective` records an event that
  `status` shows; the run's objective stays immutable, and the user starts a run with a suggestion if they like it.
  `--from RUN` lets such a run (training, stat exp, competitive sets) start from an existing game.
- **2026-11-28: A `.sav` is loaded only as a cartridge starts.** Wout wants runs to continue his own saves. The
  worker copies the file into battery RAM once, before the first frame, from a read-only bind, the way any frontend
  loads a save; there is still no operation that writes memory while a game runs, and no tool reaches this one. The
  file is checked as a whole Red/Blue save first and never changed.
- **2026-11-28: mGBA from the same pinned archive; the Gen 3 adapter scans the ROM.** `cores.py` pins `mgba_libretro.so`
  next to SameBoy (one download gets both). mGBA needs no options and boots with its built-in BIOS, so no BIOS file
  exists anywhere. Three findings shape the worker: the memory map appears only after the first frame (regions are
  taken then), `get_memory(SYSTEM_RAM)` is only EWRAM's first 32 KiB (so `ram_hash` hashes both work RAMs), and EWRAM
  is larger than one read (reads are chunked, the agent is limited to EWRAM and IWRAM).
- **2026-11-28: The Sapphire dump differs from pret.** The supplied Sapphire (header revision 2, sha1 `79bbb886…`)
  matches none of pret's three Sapphire hashes: its tables sit at other offsets and part of its code is shifted. The
  RAM layout is the same as Ruby's, so only ROM addresses are affected; names are scanned, and its few function
  addresses were taken from the same code found by its opening bytes. Never key ROM addresses by game code.
- **2026-11-28: RAM-only signals first, function addresses as optional data.** What a menu or yes/no prompt is cannot
  be told from RAM alone, so the exact ROMs carry a few function pointers; everything else (text, battle, scripts) uses
  RAM facts, and a ROM without the data degrades to a conservative `advance_text` instead of guessing.
- **2026-11-28: ROWE and Radical Red are not supported.** ROWE moves the game's RAM (its save block pointers sit at
  other addresses); Radical Red keeps FireRed's RAM but stores Pokémon unencrypted in a fixed block order with other
  table layouts and capitalisation. Both have Title Case names, so the scanner does not recognise them; a hack that
  keeps the capitals but moves RAM stops at the layout probe.
- **2026-11-29: Gen 2 uses the emulated clock.** Wout wants checkpoints, validation states and fixtures deterministic,
  so the SameBoy core runs with `sameboy_rtc: accurate`: the in-game clock follows emulated time, not the host's. A
  `--sav` has no `.rtc` file; the game continues from a fixed default time without a prompt.
- **2026-11-29: Gen 2 map names are constants files, hacks get landmarks.** pret's map identifiers (no game text) in one
  file per version family, indexed by group and number; Gold and Silver share one. A ROM that is not a known dump has no
  constants: its map is named by the ROM's landmark table (the area, e.g. a town) plus the raw group and number.
- **2026-11-29: Gen 2 recognition follows Gen 1's.** No sha1 gate: scanned tables, a header guess of the base game and a
  layout probe. Crystal 1.0, the Australian Crystal and Crystal Clear get this path and are marked unverified. The
  Spaceworld 1997 demo is another engine (DMG, WRAM bank 0, its own charmap) and would need its own adapter.
- **2026-11-29: The Gen 2 grid treats a ledge as a tile you stand on.** Live checks showed the player steps onto the hop
  tile like any floor and the next press in the hop direction jumps two squares; entering from the far side is blocked
  by the wall behind it, which is what makes ledges one-way. An earlier reading of the source (jump when stepping onto
  the tile, landing past it) was wrong and was replaced.
