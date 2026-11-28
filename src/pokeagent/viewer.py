"""Live viewers: a Tk window and a read-only web page with the latest frame, a status line and a run panel.

The game sets `frame` and `status` (the last action), the runner `panel` (`stats.status`); the window and the page
poll them. Closing the window stops watching, not the run; Ctrl-C in the terminal stops the run (SIGINT is blocked on
the window's thread so it reaches the main thread).
"""

import io
import ipaddress
import json
import signal
import threading
import tkinter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from PIL import Image, ImageTk

# Keyboard keys (Tk keysyms, lower case) to Game Boy buttons, as in common emulators.
KEYS = {
    "up": "up",
    "down": "down",
    "left": "left",
    "right": "right",
    "x": "a",
    "z": "b",
    "return": "start",
    "backspace": "select",
}
KEY_HELP = "Arrow keys move; X is A, Z is B, Enter is START, Backspace is SELECT."


def held(keys: frozenset[str], keysym: str, down: bool) -> frozenset[str]:
    """The Game Boy buttons held after a key press or release."""
    button = KEYS.get(keysym.lower())
    if button is None:
        return keys
    return keys | {button} if down else keys - {button}


class Viewer:
    def __init__(self, scale: int = 3):
        self.frame: Image.Image | None = None
        self.status = ""
        self.panel = ""
        self.buttons: frozenset[str] = frozenset()  # Game Boy buttons held on the keyboard while the window has focus
        self._error: Exception | None = None
        self.closed = threading.Event()  # set once the window is gone
        ready = threading.Event()
        threading.Thread(target=self._run, args=(scale, ready), daemon=True).start()
        ready.wait()
        if self._error:
            raise RuntimeError(f"cannot open the game window ({self._error}); --no-watch plays without one")

    def _run(self, scale: int, ready: threading.Event) -> None:
        signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
        try:
            root = tkinter.Tk()
        except tkinter.TclError as e:
            self._error = e
            self.closed.set()
            ready.set()
            return
        ready.set()
        root.title("pokeagent")
        for event, down in (("<KeyPress>", True), ("<KeyRelease>", False)):
            root.bind(event, lambda e, down=down: setattr(self, "buttons", held(self.buttons, e.keysym, down)))
        root.bind("<FocusOut>", lambda e: setattr(self, "buttons", frozenset()))  # no key stays held behind its back
        picture = tkinter.Label(root, bg="black", width=160 * scale, height=144 * scale)
        picture.pack()
        status = tkinter.Label(root, anchor="w", justify="left", font="TkFixedFont", wraplength=160 * scale)
        status.pack(fill="x")
        panel = tkinter.Label(root, anchor="nw", justify="left", font="TkFixedFont", wraplength=160 * scale)
        panel.pack(fill="both", expand=True)
        shown: list = [None, None, None]

        def tick() -> None:
            frame, text, summary = self.frame, self.status, self.panel
            if frame is not None and frame is not shown[0]:
                photo = ImageTk.PhotoImage(frame.resize((frame.width * scale, frame.height * scale), Image.NEAREST))
                picture.configure(image=photo, width=0, height=0)
                picture.photo = photo
                shown[0] = frame
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
  main { max-width: 480px; margin: 0 auto; }
  img { width: 100%; image-rendering: pixelated; background: #000; aspect-ratio: 160 / 144; display: block; }
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
