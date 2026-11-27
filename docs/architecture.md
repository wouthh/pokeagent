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
  ├─ viewer (Tk: frame + status panel)
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
  - `read(addr, n)`: non-const memory-map regions only, so ROM is refused;
  - `ram_hash`: sha256 of system RAM plus save RAM;
  - `save_state` / `load_state`.
- There is no write operation.
- Checkpoints, timelines and the persisted input log arrive with the run store (phases 4–5), so they are written
  once in their final SQLite form. The replay property they rely on (same state plus same inputs gives the same RAM
  hash and the same state) is tested now.

## Game adapter: Pokémon Red/Blue *(implemented, phase 3)*

`games/pokemon_gen1.py`, for the English Red and Blue (sha1 allowlist; any other ROM is refused). Addresses come
from the pret pokered disassembly. Pokémon, item and move names are read from the user's ROM at runtime, so no game
text is stored in the repository; `gen1_map_names.txt` holds only the map constant names.

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

## Isolation and permissions *(sandbox implemented in phase 2; the rest planned, phases 4–7)*

- `sandbox.ensure()` runs the probe once per process and raises `SandboxError` unless all of these hold:
  - network is blocked;
  - a canary file outside the mounts is absent;
  - the package mount is read-only;
  - the rlimits are in force.

  Every `Emulator` calls it first.

- Claude sessions run with only pokeagent's MCP server plus WebSearch/WebFetch (`--tools`, `--strict-mcp-config`,
  `--setting-sources ""`, `--permission-mode dontAsk`, `--disable-slash-commands`). Every session's `init` event is
  checked; unexpected tools, plugins or hooks stop the run.
- The MCP server binds to 127.0.0.1, checks host/origin, and maps each per-session bearer token to a mode. Dev
  sessions have no route to the live emulator.
- Agent-written code runs only in bubblewrap: `--unshare-all --die-with-parent --new-session --clearenv`, minimal
  read-only mounts, a tmpfs, rlimits and a wall-clock timeout. A startup probe (network connect and a read of `~`
  must fail) fails closed.
- No memory-write path exists. RAM reads are limited to RAM regions (never ROM banks).
- A PreToolUse hook on WebFetch/WebSearch logs every request and blocks payload-like URLs and private hosts.

## Objectives, memory, timelines *(planned)*

- The objective text and authoritative success checks (from the user, or the adapter for the default objective) are
  immutable. Agent-proposed checks are recorded but only pause the run for confirmation.
- Memory separates goals, milestones, knowledge (world, technical, strategy, lesson, limitation, progress; hypothesis
  versus confirmed; with provenance and scope by run, ROM sha1, game family or general), capabilities, sessions,
  events and inputs.
- Restoring a checkpoint opens a new timeline: timeline-bound state (goals, milestones, notes, progress knowledge,
  timeline KV) reverts; durable learning (knowledge, capabilities, durable KV) stays. Crash recovery replays only the
  current timeline's inputs, verified by RAM hashes.

## Capabilities *(planned)*

- **Builtin:** `get_state`, `press`, `walk_to(x, y)` (whole-map grid), `walk`, `advance_text` (stops at prompts).
- **Declarative:** RAM observations and macros, definable during play and usable in the same session.
- **Code extensions:** written in develop mode against a small `ctx` API, executed in the sandbox, activated at
  session boundaries.
- Validation is owned by the orchestrator: schemas, contracts (preconditions, postconditions, invariants, declared
  effects), a shared execution budget across nested calls, and runs on several game states. Contract violations,
  exceptions and timeouts count as runtime failures and roll a capability back automatically.

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
- A serialized state carries the whole 32 KB cartridge RAM. After an in-game save, loading the earlier state also
  restores the earlier save data, so checkpoints need no separate `.sav` and an in-game save cannot leak between
  timelines.

**Pokémon Red/Blue (English)**

- The pret pokered symbols used by the adapter are identical in the Red and Blue builds.
- After a warp, the map id changes about 30 frames before the new map's blocks are loaded; reading the walk grid
  earlier shows the old map. `step` waits until the map buffer changes and input is accepted again.
- Menus drop a direction press made in the first frames after they open; `choose` re-reads the cursor and retries.
- Oak's fades and screen transitions run longer than the gaps between text boxes, so `advance_text` treats text as
  closed only after 60 quiet frames.
- Map events can start a few frames after a step ends (Oak's event in Pallet Town), so a walk can report `arrived`
  with a dialog appearing right after it; tool results show the screen after a short settle.
- Red and Blue swap the preset names (RED/BLUE versus BLUE/RED).

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
- **2026-11-27: Game names come from the user's ROM at runtime,** not from tables in the repository, and the walk grid
  is computed from the map and tileset data in RAM and ROM instead of hand-written maps.
