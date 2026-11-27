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
- Checkpoints and timelines live in the run directory and the memory store (phases 4–5). Running the same inputs
  from the same state gives the same RAM hash and state (tested), although recovery never replays inputs.

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

## Play loop *(implemented, phase 4)*

`pokeagent play --rom PATH [--objective TEXT]` creates a run; `pokeagent resume RUN` continues one where it stopped.
Both take `--max-sessions`, `--model`, `--effort`, `--watch` (Tk window with the frame and the last
action) and `--watch-speed` (a multiple of real time; uncapped by default).

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
- **Resume** loads `recovery.state` (else the latest checkpoint, else power-on). Notes and progress are current with
  it, so nothing is replayed or restored.
- **State before a game:** until a game is started or loaded, the adapter reports only the screen, because WRAM
  holds SameBoy's randomised power-on contents.
- **Prompt:** `prompts/play.md` explains the tools, the budget, what persists and the rules (controller input
  only; web content is unverified). It names no capability to build.

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
  - token-like runs (24+ hex characters, or 32+ base64 characters mixing letters and digits), also in the hostname and
    in percent-decoded text;
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
  a restore since the last session; the current state.
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
  - *Contract, enforced by the orchestrator:* arguments against `params` and the `preconditions` before the call
    (a refusal does not count against the tool); during it one budget shared by every nested call (18,000 frames,
    600 inputs, 60 s), at most 4 nested calls, and the `invariants` after every request that ran the game; after it
    the result against `returns`, the `postconditions`, and every changed fingerprint group must be a declared
    effect. Undeclared party, bag or money changes therefore fail the call. A refused ctx request (a bad argument, a
    failed nested call) is an error the tool may catch; a budget, depth, invariant or protocol violation ends the
    call. Failures count like failed macro calls and roll a version back.
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
    brief reports. A develop session counts toward `--max-sessions`.

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
- **2026-11-27: A tool's manifest carries its validation `examples`.** The contract runs need arguments, and the
  author of the tool is the one who knows sensible ones; each is checked against `params` first.
- **2026-11-27: Review fixes for tools.** A macro a tool calls spends the tool's shared budget action by action; a
  closed protocol pipe waits only until the call's deadline; the trusted contract runs start from fresh copies of
  the real stores, so the agent's own tests cannot decide whether they pass; an existing `capabilities` table is
  rebuilt once to allow kind `tool` and status `staged`, keeping its versions.
- **2026-11-27: Review fixes for tools, round 2.** A call refuses a tool whose `compatibility.roms` excludes the
  current ROM (a family-scoped tool is visible to every ROM in the family); a nested request that fails after
  changing the game still gets the caller's invariants checked; every action's recovery snapshot also holds the
  timeline store (`recovery.tkv.json`), so an emulator restart undoes a failed action's `ctx.tkv` writes too.
