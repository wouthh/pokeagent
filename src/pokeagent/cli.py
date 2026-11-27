"""Command-line entry point."""

import argparse
import contextlib
import json
import sys
import time
from pathlib import Path

from pokeagent import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pokeagent",
        description="Autonomous, self-improving agent that plays emulated games.",
    )
    parser.add_argument("--version", action="version", version=f"pokeagent {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    commands.add_parser("fetch-cores", help="download the pinned libretro cores into the user cache (once)")
    play = commands.add_parser("play", help="start a new run on a ROM")
    play.add_argument("--rom", type=Path, required=True, help="your own ROM dump (supported games only)")
    play.add_argument("--objective", help="what the agent should achieve (default: finish the game)")
    play.add_argument(
        "--success",
        type=json.loads,
        help='your own success check as JSON, e.g. \'{"obs": "badges", "op": ">=", "value": 8}\'',
    )
    resume = commands.add_parser("resume", help="continue a run from its latest checkpoint")
    resume.add_argument("run", help="run id (a folder name under <data dir>/runs) or a run folder")
    for p in (play, resume):
        p.add_argument("--data-dir", type=Path, help="where runs live (default ~/.local/share/pokeagent)")
        p.add_argument("--max-sessions", type=int, help="stop after this many sessions (default: no limit)")
        p.add_argument("--model", help="Claude model for the sessions (default: the CLI's default)")
        p.add_argument("--effort", help="reasoning effort for the sessions")
        p.add_argument("--watch", action="store_true", help="show the game in a window")
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
                data_dir = args.data_dir or play.default_data_dir()
                run_dir = play.new_run(args.rom, args.objective, data_dir, args.success)
            else:
                run_dir, data_dir = find_run(args.run, args.data_dir)
            print(f"run {run_dir.name}: log at {run_dir / 'play.log'}")
            reason = play.play(
                run_dir,
                data_dir,
                max_sessions=args.max_sessions,
                model=args.model,
                effort=args.effort,
                watch=args.watch,
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
    elif args.command == "hook":
        from pokeagent import webhook

        return webhook.main(args.log)
    return 0
