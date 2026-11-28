"""Live viewers: a Tk window and a read-only web page with the latest frame, a status line and a run panel, and
`browse`, a read-only web browser of every run's game (latest frame and checkpoints, drawn on demand).

The game sets `frame` and `status` (the last action), the runner `panel` (`stats.status`); the window and the page
poll them. Closing the window stops watching, not the run; Ctrl-C in the terminal stops the run (SIGINT is blocked on
the window's thread so it reaches the main thread).
"""

import contextlib
import html
import io
import ipaddress
import json
import signal
import threading
import time
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from PIL import Image

SCREENS = {"sameboy": (160, 144), "mgba": (240, 160)}  # picture size by libretro core, for the window's layout

# Keyboard keys (Tk keysyms, lower case) to Game Boy buttons, as in common emulators (A and S are the GBA's L and R).
KEYS = {
    "up": "up",
    "down": "down",
    "left": "left",
    "right": "right",
    "x": "a",
    "z": "b",
    "return": "start",
    "backspace": "select",
    "a": "l",
    "s": "r",
}
KEY_HELP = "Arrow keys move; X is A, Z is B, Enter is START, Backspace is SELECT; A is L, S is R (Game Boy Advance)."


def held(keys: frozenset[str], keysym: str, down: bool) -> frozenset[str]:
    """The Game Boy buttons held after a key press or release."""
    button = KEYS.get(keysym.lower())
    if button is None:
        return keys
    return keys | {button} if down else keys - {button}


class Viewer:
    def __init__(self, size: tuple[int, int] = (160, 144)):
        self.size = size  # the picture's width and height, for the window's first layout
        self.frame: Image.Image | None = None
        self.status = ""
        self.panel = ""
        self.buttons: frozenset[str] = frozenset()  # Game Boy buttons held on the keyboard while the window has focus
        self._error: Exception | None = None
        self.closed = threading.Event()  # set once the window is gone
        ready = threading.Event()
        threading.Thread(target=self._run, args=(ready,), daemon=True).start()
        ready.wait()
        if self._error:
            raise RuntimeError(f"cannot open the game window ({self._error}); --no-watch plays without one")

    def _run(self, ready: threading.Event) -> None:
        signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
        try:
            import tkinter  # here, so a Python without Tk still runs headless and serves the web page

            from PIL import ImageTk

            root = tkinter.Tk()
        except Exception as e:  # no Tk (ImportError) or no display (TclError): report it instead of hanging init
            self._error = e
            self.closed.set()
            ready.set()
            return
        ready.set()
        root.title("pokeagent")
        for event, down in (("<KeyPress>", True), ("<KeyRelease>", False)):
            root.bind(event, lambda e, down=down: setattr(self, "buttons", held(self.buttons, e.keysym, down)))
        root.bind("<FocusOut>", lambda e: setattr(self, "buttons", frozenset()))  # no key stays held behind its back
        # The picture fills the window, scaled to the largest size that keeps the aspect (nearest neighbour).
        pw, ph = self.size
        height = max(3 * ph, root.winfo_screenheight() * 2 // 3)
        box = tkinter.Frame(root, bg="black", width=height * pw // ph, height=height)
        box.pack(fill="both", expand=True)
        box.pack_propagate(False)
        picture = tkinter.Label(box, bg="black", bd=0)
        picture.place(relx=0.5, rely=0.5, anchor="center")
        status = tkinter.Label(root, anchor="w", justify="left", font="TkFixedFont", wraplength=height * pw // ph)
        status.pack(fill="x")
        panel = tkinter.Label(root, anchor="nw", justify="left", font="TkFixedFont", wraplength=height * pw // ph)
        panel.pack(fill="x")
        shown: list = [None, None, None, None]

        def tick() -> None:
            frame, text, summary = self.frame, self.status, self.panel
            if frame is not None:
                k = min(max(box.winfo_width(), 1) / frame.width, max(box.winfo_height(), 1) / frame.height)
                size = (max(1, int(frame.width * k)), max(1, int(frame.height * k)))
                if frame is not shown[0] or size != shown[3]:
                    photo = ImageTk.PhotoImage(frame.resize(size, Image.NEAREST))
                    picture.configure(image=photo)
                    picture.photo = photo
                    shown[0], shown[3] = frame, size
            if text != shown[1]:
                status.configure(text=text)
                shown[1] = text
            if summary != shown[2]:
                panel.configure(text=summary)
                shown[2] = summary
            root.after(33, tick)

        tick()
        try:
            root.mainloop()
        finally:
            self.closed.set()


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>pokeagent</title>
<style>
  body { margin: 0; padding: 16px; background: #111; color: #ddd; font: 14px/1.4 ui-monospace, monospace; }
  main { margin: 0 auto; }
  img { width: 100%; max-height: 75vh; object-fit: contain; image-rendering: pixelated; background: #000;
        display: block; }
  pre { white-space: pre-wrap; overflow-wrap: anywhere; margin: 12px 0 0; }
  #status { color: #fff; }
</style>
</head>
<body>
<main>
<img id="frame" src="frame.png" alt="the game screen">
<pre id="status"></pre>
<pre id="panel"></pre>
</main>
<script>
const frame = document.getElementById("frame");
const next = (wait) => setTimeout(() => { frame.src = "frame.png?t=" + Date.now(); }, wait);
frame.onload = () => next(100);
frame.onerror = () => next(1000);
async function text() {
  try {
    const view = await (await fetch("status.json", { cache: "no-store" })).json();
    document.getElementById("status").textContent = view.status;
    document.getElementById("panel").textContent = view.panel;
  } catch (e) {}
  setTimeout(text, 1000);
}
text();
</script>
</body>
</html>
"""


def _local_name(host: str) -> bool:
    """Whether a Host header names this machine by address (or localhost) rather than by a DNS name, which a web
    page elsewhere could point at it (DNS rebinding)."""
    name = (host if host.endswith("]") else host.rpartition(":")[0] or host).strip("[]")
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return name == "localhost"
    return True


class _Page(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if not _local_name(self.headers.get("Host", "")):
            self.send_error(403, "open the page by this machine's address")
            return
        view, path = self.server.view, urlsplit(self.path).path
        if path == "/":
            body, kind = PAGE.encode(), "text/html; charset=utf-8"
        elif path == "/frame.png" and (frame := view.frame) is not None:
            buffer = io.BytesIO()
            frame.save(buffer, "PNG")
            body, kind = buffer.getvalue(), "image/png"
        elif path == "/status.json":
            body, kind = json.dumps({"status": view.status, "panel": view.panel}).encode(), "application/json"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args) -> None:  # quiet: requests are not worth a line each
        pass


def serve(view, address: tuple[str, int]) -> ThreadingHTTPServer:
    """Serve `view` (a Viewer, or anything with `frame`, `status` and `panel`) as a read-only web page at (host,
    port), on a daemon thread: the page, /frame.png and /status.json. Nothing on it changes the game. Call
    `shutdown()` to stop it."""
    server = ThreadingHTTPServer(address, _Page)
    server.daemon_threads = True
    server.view = view
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# ---- browsing runs -------------------------------------------------------------------------------------------------

GALLERY = 24  # checkpoints shown per run, newest first: each is drawn from its existing file, nothing is stored
PICTURES = 64  # rendered pictures kept in memory

_STYLE = """<style>
  body { margin: 0; padding: 16px; background: #111; color: #ddd; font: 14px/1.4 ui-monospace, monospace; }
  a { color: #8cf; }
  pre { white-space: pre-wrap; overflow-wrap: anywhere; }
  img { width: 100%; max-height: 75vh; object-fit: contain; image-rendering: pixelated; background: #000;
        display: block; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, 320px), 1fr)); gap: 12px; }
  figure { margin: 0; } figcaption { overflow-wrap: anywhere; }
  td { padding: 2px 12px 2px 0; vertical-align: top; }
</style>"""


def _document(title: str, body: str, head: str = "") -> bytes:
    return (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)}</title>'
        f"{_STYLE}{head}</head><body>{body}</body></html>"
    ).encode()


def _when(created: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(created))


def _render(run_dir: Path, state: bytes) -> bytes:
    """The PNG of one saved state of a run, drawn on a fresh sandboxed emulator (one frame). Nothing is stored."""
    from pokeagent import play
    from pokeagent.emulator.client import Emulator
    from pokeagent.emulator.cores import core_path

    meta, rom, _ = play._run_rom(run_dir)
    with Emulator(core_path(meta["core"]), rom, options=meta["core_options"]) as emu:
        emu.load_state(state)
        emu.run([], 1)  # draw the loaded state
        out = io.BytesIO()
        emu.frame().save(out, "PNG")
    return out.getvalue()


class _Runs(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if not _local_name(self.headers.get("Host", "")):
            self.send_error(403, "open the page by this machine's address")
            return
        found = self.server.answer(urlsplit(self.path).path)
        if found is None:
            self.send_error(404)
            return
        body, kind = found
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args) -> None:
        pass


class _RunServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], data_dir: Path):
        super().__init__(address, _Runs)
        self.data_dir = data_dir
        self.pictures: OrderedDict = OrderedDict()  # (run, file, mtime) -> PNG, the newest PICTURES
        self.places: dict = {}  # run -> ((file, mtime), where its game stands)
        self.draw = threading.Lock()  # one emulator at a time

    def answer(self, path: str) -> tuple[bytes, str] | None:
        """(body, content type) for a path, else None. Run ids and checkpoint names are looked up in the listing, never
        joined onto a path from the request."""
        from pokeagent import play

        parts = path.strip("/").split("/") if path != "/" else []
        runs = {m.parent.name: m.parent for m in (self.data_dir / "runs").glob("*/run.json")}
        if not parts:
            rows = play.run_rows(self.data_dir, self._where)
            table = "".join(
                f'<tr><td><a href="/run/{html.escape(r["id"])}">{html.escape(r["id"])}</a></td>'
                f"<td>{html.escape(r['state'])}</td><td>{r['sessions']} sessions, {r['actions']:,} actions</td>"
                f"<td>{html.escape(r['where'])}</td></tr>"
                for r in rows
            )
            return _document(
                "pokeagent runs", f"<h1>Runs</h1><table>{table or '<tr><td>no runs yet</td></tr>'}</table>"
            ), HTML
        if len(parts) < 2 or parts[0] != "run" or parts[1] not in runs:
            return None
        run_dir = runs[parts[1]]
        if len(parts) == 2:
            return self._run_page(run_dir), HTML
        with contextlib.closing(play.open_memory(run_dir, self.data_dir)) as memory:
            checkpoints = {e["data"]["file"] for e in play.restorable(run_dir, memory)}
        if parts[2:] == ["latest.png"] and (latest := play.latest_path(run_dir)):
            return self._picture(run_dir, latest), "image/png"
        name = parts[3].removesuffix(".png") if len(parts) == 4 and parts[3].endswith(".png") else None
        if parts[2] == "checkpoint" and name in checkpoints:
            return self._picture(run_dir, run_dir / "checkpoints" / name), "image/png"
        return None

    def _where(self, run_dir: Path) -> str:
        from pokeagent import play

        stamp = (latest := play.latest_path(run_dir)) and (latest.name, latest.stat().st_mtime_ns)
        if run_dir.name not in self.places or self.places[run_dir.name][0] != stamp:
            self.places[run_dir.name] = (stamp, play._where(run_dir))
        return self.places[run_dir.name][1]

    def _picture(self, run_dir: Path, path: Path) -> bytes:
        key = (run_dir.name, path.name, path.stat().st_mtime_ns)
        with self.draw:
            if key not in self.pictures:
                self.pictures[key] = _render(run_dir, path.read_bytes())
                if len(self.pictures) > PICTURES:
                    self.pictures.popitem(last=False)
            self.pictures.move_to_end(key)
            return self.pictures[key]

    def _run_page(self, run_dir: Path) -> bytes:
        from pokeagent import play, stats

        with contextlib.closing(play.open_memory(run_dir, self.data_dir)) as memory:
            panel = stats.status(memory, run_dir)
            saved = list(reversed(play.restorable(run_dir, memory)))
        name = html.escape(run_dir.name)
        figures = "".join(
            f'<figure><img loading="lazy" src="/run/{name}/checkpoint/{html.escape(e["data"]["file"])}.png"'
            f' alt="checkpoint {html.escape(e["data"]["file"])}"><figcaption>{html.escape(e["data"]["file"])}'
            f" ({html.escape(str(e['data']['reason']))}, {_when(e['created'])})"
            "</figcaption></figure>"
            for e in saved[:GALLERY]
        )
        more = f"<p>{len(saved) - GALLERY} older checkpoints not shown.</p>" if len(saved) > GALLERY else ""
        live = stats.live(run_dir)
        latest = (  # a live run gets the element even before its first state: the refresh fills it in
            f'<img id="frame" src="/run/{name}/latest.png" alt="latest game screen">'
            if live or play.latest_path(run_dir)
            else ""
        )
        refresh = ""
        if live:  # the run is playing: redraw the latest frame every couple of seconds
            refresh = (
                '<script>const f = document.getElementById("frame");'
                'if (f) setInterval(() => { f.src = "/run/' + name + '/latest.png?t=" + Date.now(); }, 2000);</script>'
            )
        body = (
            f'<p><a href="/">all runs</a></p><h1>{name}</h1>{latest}<pre>{html.escape(panel)}</pre>'
            f'<h2>Checkpoints</h2><div class="grid">{figures}</div>{more}{refresh}'
        )
        return _document(f"pokeagent {run_dir.name}", body)


HTML = "text/html; charset=utf-8"


def browse(data_dir: Path, address: tuple[str, int]) -> ThreadingHTTPServer:
    """Serve every run of `data_dir` read-only at (host, port) on a daemon thread: `/` lists the runs, `/run/<id>`
    shows one with its latest frame and checkpoints. Frames are drawn from the existing `.state` files on demand and
    kept in memory only. Call `shutdown()` to stop it."""
    server = _RunServer(address, data_dir)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
