import contextlib
import json
import sys
from io import BytesIO
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from PIL import Image

from pokeagent import cli, viewer


@pytest.fixture
def page():
    shown = SimpleNamespace(frame=None, status="walk: arrived", panel="Run r1: running")
    server = viewer.serve(shown, ("127.0.0.1", 0))
    yield shown, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def get(url, host=None, method="GET"):
    request = Request(url, method=method, headers={"Host": host} if host else {})
    with urlopen(request, timeout=5) as response:
        return response.headers["Content-Type"], response.read()


def test_the_web_page_shows_the_frame_and_status(page):
    shown, url = page
    kind, body = get(url + "/")
    assert kind.startswith("text/html") and b'src="frame.png"' in body and b"status.json" in body
    with pytest.raises(HTTPError, match="404"):
        get(url + "/frame.png")  # nothing drawn yet
    shown.frame = Image.new("RGB", (160, 144), "red")
    kind, body = get(url + "/frame.png?t=1")
    assert kind == "image/png" and Image.open(BytesIO(body)).getpixel((0, 0)) == (255, 0, 0)
    kind, body = get(url + "/status.json")
    assert json.loads(body) == {"status": "walk: arrived", "panel": "Run r1: running"}


def test_the_web_page_is_read_only_and_refuses_other_host_names(page):
    _, url = page
    for path in ("/play.log", "/../run.json", "/status.json/x"):
        with pytest.raises(HTTPError, match="404"):
            get(url + path)
    with pytest.raises(HTTPError, match="501"):
        get(url + "/status.json", method="POST")
    with pytest.raises(HTTPError, match="403"):  # a DNS name another site could point here (DNS rebinding)
        get(url + "/status.json", host="attacker.example")
    port = url.rpartition(":")[2]
    for host in (f"localhost:{port}", f"192.168.1.20:{port}", f"[::1]:{port}"):
        assert get(url + "/status.json", host=host)[0] == "application/json"


def test_web_addresses():
    assert cli.web_address("8765") == ("127.0.0.1", 8765)
    assert cli.web_address("0.0.0.0:8765") == ("0.0.0.0", 8765)
    args = cli.build_parser().parse_args(["view", "r1", "--no-watch", "--web", "8765"])
    assert (args.watch, args.web) == (False, ("127.0.0.1", 8765))
    assert cli.build_parser().parse_args(["resume", "r1", "--web", "9000"]).web == ("127.0.0.1", 9000)


def test_view_needs_somewhere_to_show_it(tmp_path):
    from pokeagent import play

    with pytest.raises(ValueError, match="nothing to show it in"):
        play.view(tmp_path, tmp_path, watch=False)


def test_keys_hold_game_boy_buttons():
    keys = viewer.held(frozenset(), "Up", True)
    keys = viewer.held(keys, "x", True)
    assert keys == {"up", "a"}
    assert viewer.held(keys, "X", False) == {"up"}  # with Shift held the keysym is upper case
    assert viewer.held(keys, "q", True) == keys  # not a game key


def test_missing_tk_is_reported_not_hung(monkeypatch):
    monkeypatch.setitem(sys.modules, "tkinter", None)  # makes `import tkinter` raise ImportError
    with pytest.raises(RuntimeError, match="cannot open the game window"):
        viewer.Viewer()


@pytest.fixture
def runs(tmp_path, monkeypatch):
    """A data dir with run r1 (two checkpoints, a latest state) and an empty run r2, served by `browse`. Frames are
    faked: a state's first byte is its grey level."""
    from pokeagent import play

    meta = {"objective": "win", "rom_sha1": "x", "family": "f", "check": None, "success": None}
    for name in ("r1", "r2"):
        (tmp_path / "runs" / name).mkdir(parents=True)
        (tmp_path / "runs" / name / "run.json").write_text(json.dumps(meta))
    monkeypatch.setattr(play, "_where", lambda run_dir: f"somewhere <{run_dir.name}>")
    monkeypatch.setattr(viewer, "_render", lambda run_dir, state: _png(state[0]))
    run_dir = tmp_path / "runs" / "r1"
    (run_dir / "checkpoints").mkdir()
    with contextlib.closing(play.open_memory(run_dir, tmp_path)) as memory:
        for i, grey in enumerate((10, 20)):
            (run_dir / "checkpoints" / f"c{i}.state").write_bytes(bytes([grey]))
            memory.event("checkpoint", {"file": f"c{i}.state", "reason": "milestone"})
    (tmp_path / "runs" / "r1" / "recovery.state").write_bytes(bytes([30]))
    server = viewer.browse(tmp_path, ("127.0.0.1", 0))
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def _png(grey):
    out = BytesIO()
    Image.new("RGB", (160, 144), (grey,) * 3).save(out, "PNG")
    return out.getvalue()


def test_browse_lists_runs_and_shows_their_frames(runs):
    kind, body = get(runs + "/")
    assert kind.startswith("text/html")
    assert b'href="/run/r1"' in body and b'href="/run/r2"' in body and b"somewhere &lt;r1&gt;" in body
    kind, body = get(runs + "/run/r1")
    assert b"/run/r1/latest.png" in body and b"/run/r1/checkpoint/c1.state.png" in body and b"Run r1" in body
    assert b"setInterval" not in body  # not live: nothing refreshes
    for path, grey in (("latest.png", 30), ("checkpoint/c0.state.png", 10)):
        kind, body = get(f"{runs}/run/r1/{path}")
        assert kind == "image/png" and Image.open(BytesIO(body)).getpixel((0, 0)) == (grey,) * 3
    assert b"latest.png" not in get(runs + "/run/r2")[1]  # r2 has no state yet


def test_browse_refreshes_the_frame_of_a_live_run(runs, tmp_path):
    import fcntl

    with (tmp_path / "runs" / "r1" / ".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        assert b"setInterval" in get(runs + "/run/r1")[1]
    (tmp_path / "runs" / "r2" / ".lock").touch()
    with (tmp_path / "runs" / "r2" / ".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        assert b'id="frame"' in get(runs + "/run/r2")[1]  # live before its first state: the refresh fills it in


def test_browse_is_read_only_and_only_serves_listed_names(runs):
    for path in (
        "/run/nope",
        "/run/r1/checkpoint/nope.png",
        "/run/r1/checkpoint/c0.state",
        "/run/r2/latest.png",
        "/run/../r1",
        "/run/r1/checkpoint/..%2Frun.json.png",
        "/run/r1/checkpoint/../../run.json",
        "/run/%2e%2e/r1",
        "/other",
    ):
        with pytest.raises(HTTPError, match="404"):
            get(runs + path)
    with pytest.raises(HTTPError, match="501"):
        get(runs + "/", method="POST")
    with pytest.raises(HTTPError, match="403"):
        get(runs + "/", host="attacker.example")


def test_web_command_address():
    args = cli.build_parser().parse_args(["web"])
    assert args.address == ("127.0.0.1", 8765)
    assert cli.build_parser().parse_args(["web", "0.0.0.0:9000"]).address == ("0.0.0.0", 9000)
