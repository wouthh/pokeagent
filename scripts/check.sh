#!/bin/sh
# Local gate: lint, format, tests, and no ROM/save/state files tracked.
# Prints only failures, and one line when everything passed.
set -eu
cd "$(dirname "$0")/.."
uv run --frozen --quiet ruff check --quiet .
uv run --frozen --quiet ruff format --check --quiet .
out=$(uv run --frozen --quiet pytest -q -p no:cacheprovider 2>&1) || { printf '%s\n' "$out" >&2; exit 1; }
tracked=$(git ls-files | grep -iE '\.(gb|gbc|gba|nds|sav|srm|state)$' | grep -vx 'tests/fixtures/dmg-acid2.gb' || true)
if [ -n "$tracked" ]; then
    printf 'ROM, save or state files are tracked:\n%s\n' "$tracked" >&2
    exit 1
fi
echo "check passed"
