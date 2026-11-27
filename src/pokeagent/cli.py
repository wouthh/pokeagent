"""Command-line entry point."""

import argparse
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
    elif args.command == "hook":
        from pokeagent import webhook

        return webhook.main(args.log)
    return 0
