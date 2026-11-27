import io
import json

import pytest

from pokeagent import webhook


@pytest.mark.parametrize(
    "url",
    [
        "https://bulbapedia.bulbagarden.net/wiki/Pok%C3%A9mon_Red_and_Blue_Versions",
        "https://www.serebii.net/rb/pokemon.shtml",
        "https://github.com/pret/pokered/blob/master/ram/wram.asm",
        "https://example.com/pokemon-red-blue-walkthrough-part-one-pallet-town-to-viridian",
    ],
)
def test_allows_ordinary_pages(url):
    assert webhook.decide("WebFetch", {"url": url}) is None


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8080/",
        "http://127.0.0.1/mcp",
        "http://127.1/",
        "http://0x7f.0.0.1/",
        "http://0x7f.1/",
        "http://127.0.0.1./",
        "http://10.0.0.5/",
        "http://192.168.1.1/admin",
        "http://[::1]/",
        "http://169.254.169.254/latest/meta-data/",
        "http://router.lan/",
        "http://intranet/",
        "file:///etc/passwd",
        "ftp://example.com/x",
        "https://user:pw@example.com/",
        "https://example.com/?q=" + "a" * 250,
        "https://example.com/c/3f9a2b7c4d5e6f708192a3b4c5d6e7f8",
        "https://example.com/?d=U2FsdGVkX19vY2tlbnRva2VuMTIzNDU2Nzg5MA==",
    ],
)
def test_blocks_local_and_payload_urls(url):
    assert webhook.decide("WebFetch", {"url": url}) is not None


def test_search_queries():
    assert webhook.decide("WebSearch", {"query": "pokemon red how to get to cerulean city"}) is None
    assert webhook.decide("WebSearch", {"query": "x " * 150}) is not None
    assert webhook.decide("WebSearch", {"query": "deadbeefcafebabe0123456789abcdef"}) is not None
    assert webhook.decide("Bash", {"command": "ls"}) is not None


def test_main_logs_and_blocks(tmp_path, monkeypatch, capsys):
    log = tmp_path / "web.jsonl"
    for payload, code in [
        ({"tool_name": "WebFetch", "tool_input": {"url": "https://serebii.net/"}}, 0),
        ({"tool_name": "WebFetch", "tool_input": {"url": "http://127.0.0.1/"}}, 2),
        ("not json", 2),
    ]:
        monkeypatch.setattr("sys.stdin", io.StringIO(payload if isinstance(payload, str) else json.dumps(payload)))
        assert webhook.main(log) == code
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    assert [e["blocked"] is None for e in entries] == [True, False, False]
    assert "web policy" in capsys.readouterr().err


@pytest.mark.parametrize("url", ["http://127.0.0.1/", "https://serebii.net/"])
def test_main_blocks_when_log_unwritable(tmp_path, monkeypatch, url):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"tool_name": "WebFetch", "tool_input": {"url": url}})))
    assert webhook.main(tmp_path / "missing" / "web.jsonl") == 2
