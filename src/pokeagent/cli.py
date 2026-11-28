"""Command-line entry point."""

import argparse
import contextlib
import json
import sys
import time
from pathlib import Path

from pokeagent import __version__


def web_address(text: str) -> tuple[str, int]:
    """[HOST:]PORT for --web; the host defaults to this machine only."""
    host, _, port = text.rpartition(":")
    return host or "127.0.0.1", int(port)


WEB_HELP = (
    "also show the game on a web page at [HOST:]PORT, e.g. 8765 (this machine only) or 0.0.0.0:8765 (other devices"
    " on your network, if the firewall lets them in)"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pokeagent",
        description="Autonomous, self-improving agent that plays emulated games.",
    )
    parser.add_argument("--version", action="version", version=f"pokeagent {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    commands.add_parser("fetch-cores", help="download the pinned libretro cores into the user cache (once)")
    play = commands.add_parser("play", help="start a new run on a ROM")
    play.add_argument("--rom", type=Path, help="your own ROM dump (supported games only)")
    play.add_argument(
        "--from",
        dest="source",
        metavar="RUN",
        help="start from another run's latest game (its ROM; that run does not change) instead of power-on",
    )
    play.add_argument("--objective", help="what the agent should achieve (default: finish the game)")
    play.add_argument(
        "--success",
        type=json.loads,
        help='your own success check as JSON, e.g. \'{"obs": "badges", "op": ">=", "value": 8}\'',
    )
    play.add_argument(
        "--style",
        choices=("natural", "direct"),
        default="natural",
        help="natural (default): catch new species, a varied and evenly trained team, items, nicknames and side"
        " quests; direct: the objective by the shortest route",
    )
    play.add_argument("--glitches", action="store_true", help="allow the game's known glitches (default: off)")
    resume = commands.add_parser("resume", help="continue a run from its latest checkpoint")
    resume.add_argument("run", help="run id (a folder name under <data dir>/runs) or a run folder")
    for p in (play, resume):
        p.add_argument("--data-dir", type=Path, help="where runs live (default ~/.local/share/pokeagent)")
        p.add_argument("--max-sessions", type=int, help="stop after this many sessions (default: no limit)")
        p.add_argument("--model", help="Claude model for the sessions (default: the CLI's default)")
        p.add_argument("--effort", help="reasoning effort for the sessions")
        p.add_argument(
            "--watch",
            action=argparse.BooleanOptionalAction,
            default=True,
            help="show the game in a window (default); --no-watch plays without one",
        )
        p.add_argument("--web", type=web_address, metavar="[HOST:]PORT", help=WEB_HELP)
        p.add_argument("--watch-speed", type=float, help="emulation speed, a multiple of real time (default: uncapped)")
        p.add_argument("--dev-share", type=float, default=0.3, help="most of the run's tokens development may use")
        p.add_argument("--dev-attempts", type=int, default=2, help="development sessions per tool")
        p.add_argument("--dev-timeout", type=float, default=45, help="minutes per development session")
        p.add_argument("--dev-validations", type=int, default=10, help="validations per development session")
    objective = commands.add_parser("objective", help="settle a run's completion claim or approve its proposed check")
    objective.add_argument("action", choices=["confirm", "reject", "approve-check"])
    objective.add_argument("run", help="run id or run folder")
    objective.add_argument("--reason", default="", help="for reject: what is still missing (the agent reads it)")
    objective.add_argument("--data-dir", type=Path, help="where runs live (default ~/.local/share/pokeagent)")
    restore = commands.add_parser("restore", help="take a stopped run back to one of its checkpoints")
    restore.add_argument("run", help="run id or run folder")
    restore.add_argument("checkpoint", nargs="?", help="checkpoint file name (omit to list them)")
    restore.add_argument("--data-dir", type=Path, help="where runs live (default ~/.local/share/pokeagent)")
    capability = commands.add_parser("capability", help="list the observations and macros a run sees, or roll one back")
    capability.add_argument("action", choices=["list", "rollback"])
    capability.add_argument("run", help="run id or run folder")
    capability.add_argument("name", nargs="?", help="for rollback: the capability to take back to its previous version")
    capability.add_argument("--data-dir", type=Path, help="where runs live (default ~/.local/share/pokeagent)")
    view = commands.add_parser("view", help="show a run's game in a window without playing it")
    view.add_argument("run", help="run id or run folder")
    view.add_argument("checkpoint", nargs="?", help="checkpoint file name (default: the latest state)")
    view.add_argument("--watch", action=argparse.BooleanOptionalAction, default=True, help="show the window (default)")
    view.add_argument("--web", type=web_address, metavar="[HOST:]PORT", help=WEB_HELP)
    view.add_argument(
        "--keyboard", action="store_true", help="play a copy yourself with the keyboard; the run does not change"
    )
    view.add_argument("--out", type=Path, help="with --keyboard: write your in-game save here as a .sav")
    view.add_argument("--force", action="store_true", help="with --out: replace the file if it exists")
    view.add_argument("--data-dir", type=Path, help="where runs live (default ~/.local/share/pokeagent)")
    export = commands.add_parser("export-sav", help="save a run's game in game and write it as a .sav file")
    export.add_argument("run", help="run id or run folder")
    export.add_argument("checkpoint", nargs="?", help="checkpoint file name (default: the newest state it can save)")
    export.add_argument("--out", type=Path, help="where to write it (default: <ROM name>.sav in the run folder)")
    export.add_argument("--force", action="store_true", help="replace --out if it exists")
    export.add_argument("--data-dir", type=Path, help="where runs live (default ~/.local/share/pokeagent)")
    web = commands.add_parser(
        "web", help="browse every run's game (latest frame and checkpoints) on a read-only web page"
    )
    web.add_argument("address", nargs="?", default="8765", type=web_address, metavar="[HOST:]PORT", help=WEB_HELP)
    web.add_argument("--data-dir", type=Path, help="where runs live (default ~/.local/share/pokeagent)")
    runs = commands.add_parser("runs", help="list the runs and where each one's game stands")
    runs.add_argument("--data-dir", type=Path, help="where runs live (default ~/.local/share/pokeagent)")
    status = commands.add_parser("status", help="where a run stands")
    stats = commands.add_parser("stats", help="per-session metrics of a run, optionally compared with another")
    stats.add_argument("--compare", metavar="RUN", help="another run of the same data dir, compared at milestones")
    for p in (status, stats):
        p.add_argument("run", help="run id or run folder")
        p.add_argument("--data-dir", type=Path, help="where runs live (default ~/.local/share/pokeagent)")
    hook = commands.add_parser("hook", help="hooks called by Claude sessions (internal)")
    hook.add_argument("kind", choices=["web"])
    hook.add_argument("--log", type=Path, required=True)
    return parser


def find_run(run: str, data_dir: Path | None) -> tuple[Path, Path]:
    """The run folder and its owning data dir: a run folder path names its own `<data dir>/runs/<id>` owner
    unless `--data-dir` says otherwise."""
    if "/" in run:
        run_dir = Path(run).resolve()
        data_dir = data_dir or run_dir.parent.parent
    else:
        from pokeagent import play

        data_dir = data_dir or play.default_data_dir()
        run_dir = data_dir / "runs" / run
    if not (run_dir / "run.json").exists():
        raise ValueError(f"no run at {run_dir}")
    return run_dir, data_dir


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "fetch-cores":
        from pokeagent.emulator.cores import cores_dir, fetch_cores

        if installed := fetch_cores():
            print(f"installed {', '.join(installed)} into {cores_dir()}")
    elif args.command == "objective":
        from pokeagent import objectives, play

        try:
            run_dir, data_dir = find_run(args.run, args.data_dir)
            with contextlib.closing(play.open_memory(run_dir, data_dir)) as memory:
                if args.action == "confirm":
                    print(f"run {run_dir.name} complete: {objectives.confirm(memory)}")
                elif args.action == "reject":
                    objectives.reject(memory, args.reason)
                    print(f"claim rejected; continue with `pokeagent resume {run_dir.name}`")
                else:
                    print(f"approved as a user check: {objectives.describe(objectives.approve_check(memory))}")
        except (OSError, ValueError) as e:
            print(f"pokeagent: {e}", file=sys.stderr)
            return 1
    elif args.command == "restore":
        from pokeagent import play

        try:
            run_dir, data_dir = find_run(args.run, args.data_dir)
            with play.run_lock(run_dir), contextlib.closing(play.open_memory(run_dir, data_dir)) as memory:
                if args.checkpoint:
                    done = play.restore(run_dir, memory, args.checkpoint)
                    print(f"restored {done['file']}; {done['undone_actions']} actions undone, on a new timeline")
                else:
                    for event in play.restorable(run_dir, memory):
                        saved = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(event["created"]))
                        print(
                            f"{event['data']['file']}  {saved}  timeline {event['timeline']}  {event['data']['reason']}"
                        )
        except (OSError, ValueError) as e:
            print(f"pokeagent: {e}", file=sys.stderr)
            return 1
    elif args.command == "capability":
        from pokeagent import capabilities, play

        try:
            run_dir, data_dir = find_run(args.run, args.data_dir)
            with contextlib.closing(play.open_memory(run_dir, data_dir)) as memory:
                registry = capabilities.Registry(memory)
                if args.action == "list":
                    for found in registry.active():
                        print(capabilities.summary(registry, found))
                else:
                    found = next((c for c in registry.active() if c["name"] == args.name), None)
                    if found is None:
                        raise ValueError(f"no active capability named {args.name!r}; see `pokeagent capability list`")
                    print(registry.rollback(found, "by the user"))
        except (OSError, ValueError) as e:
            print(f"pokeagent: {e}", file=sys.stderr)
            return 1
    elif args.command in ("play", "resume"):
        from pokeagent import play

        try:
            if args.command == "play":
                if bool(args.rom) == bool(args.source):
                    raise ValueError("give either --rom (a new game) or --from RUN (that run's game)")
                source, data_dir = find_run(args.source, args.data_dir) if args.source else (None, None)
                data_dir = args.data_dir or data_dir or play.default_data_dir()
                run_dir = play.new_run(
                    args.rom, args.objective, data_dir, args.success, args.style, args.glitches, source
                )
            else:
                run_dir, data_dir = find_run(args.run, args.data_dir)
            print(f"run {run_dir.name}: log at {run_dir / 'play.log'}")
            if args.web:
                print(f"web page at http://{args.web[0]}:{args.web[1]}/", flush=True)
            reason = play.play(
                run_dir,
                data_dir,
                max_sessions=args.max_sessions,
                model=args.model,
                effort=args.effort,
                watch=args.watch,
                web=args.web,
                speed=args.watch_speed,
                development={
                    "dev_share": args.dev_share,
                    "dev_attempts": args.dev_attempts,
                    "dev_timeout": args.dev_timeout * 60,
                    "dev_validations": args.dev_validations,
                },
            )
        except (OSError, ValueError, RuntimeError) as e:  # includes ProviderError, SandboxError, EmulatorError
            print(f"pokeagent: {e}", file=sys.stderr)
            return 1
        print(f"run {run_dir.name} stopped: {reason}")
    elif args.command in ("view", "export-sav"):
        from pokeagent import play

        try:
            run_dir, data_dir = find_run(args.run, args.data_dir)
            if args.command == "view":
                if args.web:
                    print(f"web page at http://{args.web[0]}:{args.web[1]}/", flush=True)
                if args.keyboard:
                    print(play.play_by_hand(run_dir, data_dir, args.checkpoint, args.out, args.force, args.web))
                else:
                    play.view(run_dir, data_dir, args.checkpoint, args.watch, args.web)
            else:
                rom = Path(json.loads((run_dir / "run.json").read_text())["rom"])
                out = args.out or run_dir / f"{rom.stem}.sav"
                used = play.export_save(run_dir, data_dir, out, args.checkpoint, args.force)
                print(f"saved in game at {used}; wrote {out}")
        except (OSError, ValueError, RuntimeError) as e:  # includes SandboxError and EmulatorError
            print(f"pokeagent: {e}", file=sys.stderr)
            return 1
    elif args.command == "web":
        import threading

        from pokeagent import play, viewer

        try:
            server = viewer.browse(args.data_dir or play.default_data_dir(), args.address)
        except OSError as e:
            print(f"pokeagent: {e}", file=sys.stderr)
            return 1
        print(f"runs at http://{args.address[0]}:{server.server_address[1]}/ (Ctrl-C to stop)", flush=True)
        with contextlib.suppress(KeyboardInterrupt):
            threading.Event().wait()
        server.shutdown()
    elif args.command == "runs":
        from pokeagent import play

        print(play.list_runs(args.data_dir or play.default_data_dir()))
    elif args.command in ("status", "stats"):
        from pokeagent import play, stats

        try:
            run_dir, data_dir = find_run(args.run, args.data_dir)
            with contextlib.closing(play.open_memory(run_dir, data_dir)) as memory:
                if args.command == "status":
                    print(stats.status(memory, run_dir))
                else:
                    print(stats.report(memory, stats.live(run_dir)))
                    if args.compare:
                        other_dir, _ = find_run(args.compare, data_dir)
                        with contextlib.closing(play.open_memory(other_dir, data_dir)) as other:
                            print()
                            print(stats.compare(memory, other))
        except (OSError, ValueError) as e:
            print(f"pokeagent: {e}", file=sys.stderr)
            return 1
    elif args.command == "hook":
        from pokeagent import webhook

        return webhook.main(args.log)
    return 0
