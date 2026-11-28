import json
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
