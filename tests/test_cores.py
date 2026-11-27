import hashlib
import subprocess
from pathlib import Path

import pytest

from pokeagent.emulator import cores


def _fake_archive(tmp_path: Path, payload: bytes) -> Path:
    member = tmp_path / "src" / cores.MEMBER_DIR / "fake_libretro.so"
    member.parent.mkdir(parents=True)
    member.write_bytes(payload)
    archive = tmp_path / "cores.7z"
    subprocess.run(["7z", "a", "-bso0", "-bsp0", str(archive), cores.MEMBER_DIR], cwd=tmp_path / "src", check=True)
    return archive


@pytest.mark.parametrize("corrupt", ["none", "archive", "member"])
def test_fetch_verifies_checksums(tmp_path, monkeypatch, corrupt):
    payload = b"\x7fELF fake core"
    archive = _fake_archive(tmp_path, payload)
    archive_sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    payload_sha = hashlib.sha256(payload).hexdigest()
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(cores, "ARCHIVE_URL", archive.as_uri())
    monkeypatch.setattr(cores, "ARCHIVE_SHA256", "0" * 64 if corrupt == "archive" else archive_sha)
    monkeypatch.setattr(
        cores, "CORES", {"fake": ("fake_libretro.so", "0" * 64 if corrupt == "member" else payload_sha)}
    )
    if corrupt != "none":
        with pytest.raises(RuntimeError, match="checksum"):
            cores.fetch_cores()
        assert not (cores.cores_dir() / "fake_libretro.so").exists()
        return
    assert cores.fetch_cores() == ["fake"]
    assert cores.core_path("fake").read_bytes() == payload
    assert cores.fetch_cores() == []
    assert [p.name for p in cores.cores_dir().iterdir()] == ["fake_libretro.so"]


def test_missing_core_names_the_fix(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    with pytest.raises(FileNotFoundError, match="fetch-cores"):
        cores.core_path("sameboy")
