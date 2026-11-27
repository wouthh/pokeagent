"""Command-line entry point."""

import argparse

from pokeagent import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pokeagent",
        description="Autonomous, self-improving agent that plays emulated games.",
    )
    parser.add_argument("--version", action="version", version=f"pokeagent {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    build_parser().parse_args(argv)
    return 0
