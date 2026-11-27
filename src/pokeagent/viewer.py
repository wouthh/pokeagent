"""Live viewer: a Tk window with the latest frame and a status line, on its own thread.

The game sets `frame` and `status`; the window polls them. Closing the window stops watching, not the run; Ctrl-C in
the terminal stops the run (SIGINT is blocked on this thread so it reaches the main thread).
"""

import signal
import threading
import tkinter

from PIL import Image, ImageTk


class Viewer:
    def __init__(self, scale: int = 3):
        self.frame: Image.Image | None = None
        self.status = ""
        self._error: Exception | None = None
        ready = threading.Event()
        threading.Thread(target=self._run, args=(scale, ready), daemon=True).start()
        ready.wait()
        if self._error:
            raise RuntimeError(f"cannot open the viewer window: {self._error}")

    def _run(self, scale: int, ready: threading.Event) -> None:
        signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
        try:
            root = tkinter.Tk()
        except tkinter.TclError as e:
            self._error = e
            ready.set()
            return
        ready.set()
        root.title("pokeagent")
        picture = tkinter.Label(root, bg="black", width=160 * scale, height=144 * scale)
        picture.pack()
        status = tkinter.Label(root, anchor="w", justify="left", font="TkFixedFont", wraplength=160 * scale)
        status.pack(fill="x")
        shown: list = [None, None]

        def tick() -> None:
            frame, text = self.frame, self.status
            if frame is not None and frame is not shown[0]:
                photo = ImageTk.PhotoImage(frame.resize((frame.width * scale, frame.height * scale), Image.NEAREST))
                picture.configure(image=photo, width=0, height=0)
                picture.photo = photo
                shown[0] = frame
            if text != shown[1]:
                status.configure(text=text)
                shown[1] = text
            root.after(33, tick)

        tick()
        root.mainloop()
