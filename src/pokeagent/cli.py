"""Command-line entry point."""

import argparse
import sys
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
    resume = commands.add_parser("resume", help="continue a run from its latest checkpoint")
    resume.add_argument("run", help="run id (a folder name under <data dir>/runs) or a run folder")
    for p in (play, resume):
        p.add_argument("--data-dir", type=Path, help="where runs live (default ~/.local/share/pokeagent)")
        p.add_argument("--max-sessions", type=int, help="stop after this many sessions (default: no limit)")
        p.add_argument("--model", help="Claude model for the sessions (default: the CLI's default)")
        p.add_argument("--effort", help="reasoning effort for the sessions")
        p.add_argument("--watch", action="store_true", help="show the game in a window")
        p.add_argument("--watch-speed", type=float, help="emulation speed, a multiple of real time (default: uncapped)")
    hook = commands.add_parser("hook", help="hooks called by Claude sessions (internal)")
    hook.add_argument("kind", choices=["web"])
    hook.add_argument("--log", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "fetch-cores":
        from pokeagent.emulator.cores import cores_dir, fetch_cores

        if installed := fetch_cores():
            print(f"installed {', '.join(installed)} into {cores_dir()}")
    elif args.command in ("play", "resume"):
        from pokeagent import play

        data_dir = args.data_dir or play.default_data_dir()
        try:
            if args.command == "play":
                run_dir = play.new_run(args.rom, args.objective, data_dir)
            else:
                run_dir = Path(args.run) if "/" in args.run else data_dir / "runs" / args.run
                if not (run_dir / "run.json").exists():
                    raise ValueError(f"no run at {run_dir}")
            print(f"run {run_dir.name}: log at {run_dir / 'play.log'}")
            reason = play.play(
                run_dir,
                data_dir,
                max_sessions=args.max_sessions,
                model=args.model,
                effort=args.effort,
                watch=args.watch,
                speed=args.watch_speed,
            )
        except (OSError, ValueError, RuntimeError) as e:  # includes ProviderError, SandboxError, EmulatorError
            print(f"pokeagent: {e}", file=sys.stderr)
            return 1
        print(f"run {run_dir.name} stopped: {reason}")
    elif args.command == "hook":
        from pokeagent import webhook

        return webhook.main(args.log)
    return 0
