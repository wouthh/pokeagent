"""Pinned libretro cores, downloaded once per machine into the user's cache.

Source: RetroArch's stable cores archive (pinned by sha256); only the needed `.so` files are extracted and the
archive is deleted. See docs/architecture.md, Decisions.
"""

import hashlib
import os
import subprocess
import tempfile
import urllib.request
from pathlib import Path

ARCHIVE_URL = "https://buildbot.libretro.com/stable/1.22.2/linux/x86_64/RetroArch_cores.7z"
ARCHIVE_SHA256 = "4b7ed8dc97d4bf035fce182c64b5658c7662e2e9e5d42129538afbd4b6096307"
MEMBER_DIR = "RetroArch-Linux-x86_64/RetroArch-Linux-x86_64.AppImage.home/.config/retroarch/cores/"
CORES = {
    "sameboy": ("sameboy_libretro.so", "2de48824e35a276695cd5d7c7293926e4e0920a09c3f4c0bd6c53eba350799fc"),
    "mgba": ("mgba_libretro.so", "564c32a7214fe52f7d3b7ead5fbe70a7e45b5ccb3b52da2c07bc1b6149de75a5"),
}


def cores_dir() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "pokeagent" / "cores"


def _sha256(path: Path) -> str:
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def core_path(name: str) -> Path:
    """Path of an installed core, verified against its pinned checksum."""
    file, sha = CORES[name]
    path = cores_dir() / file
    if not path.is_file():
        raise FileNotFoundError(f"libretro core {name!r} is not installed; run `pokeagent fetch-cores`")
    if _sha256(path) != sha:
        raise RuntimeError(f"{path} does not match its pinned checksum; delete it and run `pokeagent fetch-cores`")
    return path


def fetch_cores() -> list[str]:
    """Download and install missing cores; returns the names installed (empty when all are present)."""
    target = cores_dir()
    missing = {n: v for n, v in CORES.items() if not ((target / v[0]).is_file() and _sha256(target / v[0]) == v[1])}
    if not missing:
        return []
    target.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target) as tmp:
        archive = Path(tmp) / "cores.7z"
        digest = hashlib.sha256()
        with urllib.request.urlopen(ARCHIVE_URL, timeout=60) as response, archive.open("wb") as f:
            while chunk := response.read(1 << 20):
                digest.update(chunk)
                f.write(chunk)
        if digest.hexdigest() != ARCHIVE_SHA256:
            raise RuntimeError(f"{ARCHIVE_URL} does not match its pinned checksum")
        for file, sha in missing.values():
            subprocess.run(
                ["7z", "e", "-y", "-bso0", "-bsp0", f"-o{tmp}", str(archive), MEMBER_DIR + file],
                check=True,
                stdin=subprocess.DEVNULL,
            )
            extracted = Path(tmp) / file
            if _sha256(extracted) != sha:
                raise RuntimeError(f"{file} from the archive does not match its pinned checksum")
            extracted.chmod(0o644)
            extracted.replace(target / file)
    return list(missing)
