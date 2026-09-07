#!/usr/bin/env python3
"""A control socket for the editor, so a problem can be reproduced instead of
described.

    hdrmerge-timelapser edit FOLDER --serve

Off unless asked for, bound to 127.0.0.1 and to nothing else. It exists so that
whoever is helping with a bug can drive the same window the reporter is looking
at: press the keys, read the state, take the picture. A screenshot answers in
one round trip what a paragraph cannot -- what the window actually shows.

    curl localhost:8765/state
    curl -X POST localhost:8765/key  -d '{"key": "down"}'
    curl -X POST localhost:8765/call -d '{"name": "toggle_step", "args": [0]}'
    curl -o shot.png localhost:8765/shot

Qt objects belong to the thread that made them, so nothing here touches a
widget. Every request drops a callable on a queue and waits; a timer inside the
GUI thread drains it, runs it there, and hands the answer back. The wait has a
timeout, so a hung window returns an error rather than a hung client.
"""

import json
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import plan

# What `/call` may reach. A whitelist and not getattr on anything: this is a
# debugging aid on a socket, and the smallest useful surface is the right one.
CALLABLE = (
    "show_frame", "toggle_step", "toggle_include",
    "cycle_radius", "set_radius", "revert", "undo_last", "align_steps", "align_mode", "apply_align", "cancel_align", "reset_offsets", "reset_offset_one", "autodetect", "select_all", "aim_step", "nudge",
    "merge_now", "show_step", "save", "reload_list",
    "zoom_in", "zoom_out", "zoom_fit", "set_exposure", "toggle_marks",
    "nudge_exposure", "set_detail", "reset_tone", "go", "step_row", "toggle_singles",
    "redo_last", "set_view",
    "set_view",
    )

KEYS = {}


def _qt():
    from PySide6 import QtCore, QtGui, QtWidgets
    return QtCore, QtGui, QtWidgets


def _key_table():
    """Name to key code: "down", "space", "r", "ctrl+s" is done with mods.

    Built from `Qt.Key.__members__` and not from `dir(Qt)`: the names live on
    the enum, not loose on the namespace, and scanning the namespace finds none
    of them.
    """
    if KEYS:
        return KEYS
    QtCore, _, _ = _qt()
    for name, value in QtCore.Qt.Key.__members__.items():
        KEYS[name[4:].lower()] = value
    return KEYS


class _Bridge:
    """Runs callables on the GUI thread and hands back what they return."""

    def __init__(self, editor):
        QtCore, _, _ = _qt()
        self.editor = editor
        self.jobs = queue.Queue()
        self.timer = QtCore.QTimer(editor.win)
        self.timer.timeout.connect(self._drain)
        self.timer.start(20)

    def _drain(self):
        while True:
            try:
                fn, box, done = self.jobs.get_nowait()
            except queue.Empty:
                return
            try:
                box.append(("ok", fn()))
            except Exception as e:                          # noqa: BLE001
                box.append(("error", "%s: %s" % (type(e).__name__, e)))
            done.set()

    def run(self, fn, timeout=60.0):
        _, _, QtWidgets = _qt()
        modal = QtWidgets.QApplication.activeModalWidget()
        if modal is not None:
            return "error", ("a dialog is waiting: %r"
                             % (modal.windowTitle() or type(modal).__name__))
        box, done = [], threading.Event()
        self.jobs.put((fn, box, done))
        if not done.wait(timeout):
            return "error", "timed out waiting for the window"
        return box[0]


def _state(editor):
    """Everything the window knows, so a helper can see it without a screen."""
    f = editor.frame
    return {
        "folder": editor.folder,
        "plan": editor.path,
        "frame": editor.i + 1,
        "frames": len(editor.doc["frames"]),
        "anchor": f["anchor"],
        "time": f["time"],
        "kind": f["kind"],
        "include": f["include"],
        "steps": [{"frame": s["frame"], "use": s["use"],
                   "offset": s.get("offset"),
                   "serves_percent": s.get("serves_percent")}
                  for s in f["steps"]],
        "aim": editor.aim,
        "aligning": editor.aligning,
        "merged_from": plan.merged_steps(f),
        "blend_radius": f.get("blend_radius"),
        "borrow": f.get("borrow"),
        "manual": f.get("manual", []),
        # Where the window is, so a recording can be cropped to it rather
        # than to a rectangle somebody guessed.
        "window": [editor.win.x(), editor.win.y(),
                   editor.win.width(), editor.win.height()],
        "dirty": editor.dirty,
        "status": editor.msg_text,
        "summary": plan.summary(editor.doc),
    }


def _shot(editor):
    QtCore, _, _ = _qt()
    buf = QtCore.QBuffer()
    buf.open(QtCore.QIODevice.WriteOnly)
    editor.win.grab().save(buf, "PNG")
    return bytes(buf.data())


def _press(editor, key, mods):
    """Press a key the way the keyboard would, shortcuts included.

    A synthesised event is not enough on its own. Qt matches shortcuts on the
    way in from the platform, in `QShortcutMap`, before anything is delivered
    as an event -- so posting a key event reaches the window's filters (the
    held masks, shift with a digit) and misses every `QShortcut` there is.
    Which made this socket quietly useless for testing exactly the keys the
    sheet advertises. The shortcuts are therefore looked up by their own key
    sequence and fired, and the event is sent as well for whatever reads it
    directly.
    """
    QtCore, QtGui, QtWidgets = _qt()
    code = _key_table().get(str(key).lower())
    if code is None:
        raise ValueError("unknown key %r" % key)
    flags = QtCore.Qt.NoModifier
    for m in mods or []:
        flags |= {"shift": QtCore.Qt.ShiftModifier,
                  "ctrl": QtCore.Qt.ControlModifier,
                  "alt": QtCore.Qt.AltModifier,
                  "meta": QtCore.Qt.MetaModifier}[m.lower()]
    target = QtWidgets.QApplication.focusWidget() or editor.win
    fired = False
    want = QtGui.QKeySequence(int(code) | int(flags.value if hasattr(flags, "value")
                                              else flags))
    for sc in editor.win.findChildren(QtGui.QShortcut):
        if sc.isEnabled() and sc.key().matches(want) == QtGui.QKeySequence.ExactMatch:
            sc.activated.emit()
            fired = True
    if not fired:
        for kind in (QtCore.QEvent.KeyPress, QtCore.QEvent.KeyRelease):
            QtWidgets.QApplication.sendEvent(
                target, QtGui.QKeyEvent(kind, code, flags))
    return _state(editor)


def _call(editor, name, args):
    if name not in CALLABLE:
        raise ValueError("%s is not on the list of things this may call" % name)
    getattr(editor, name)(*(args or []))
    return _state(editor)


def serve(editor, port=8765):
    """Start the control socket. Returns the port it is listening on."""
    bridge = _Bridge(editor)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):        # the terminal belongs to the user
            pass

        def _send(self, code, body, kind="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(
                body, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _reply(self, fn, kind="application/json"):
            how, what = bridge.run(fn)
            if how == "error":
                return self._send(500, {"error": what})
            self._send(200, what, kind)

        def do_GET(self):
            if self.path.startswith("/shot"):
                return self._reply(lambda: _shot(bridge.editor), "image/png")
            if self.path.startswith("/state"):
                return self._reply(lambda: _state(bridge.editor))
            if self.path.startswith("/plan"):
                return self._reply(lambda: bridge.editor.doc)
            self._send(404, {"error": "try /state, /plan, /shot, /key, /call"})

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError as e:
                return self._send(400, {"error": "bad JSON: %s" % e})
            editor = bridge.editor
            if self.path.startswith("/key"):
                return self._reply(lambda: _press(editor, body.get("key"),
                                                  body.get("mods")))
            if self.path.startswith("/call"):
                return self._reply(lambda: _call(editor, body.get("name"),
                                                 body.get("args")))
            self._send(404, {"error": "try /key or /call"})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print("control socket on http://127.0.0.1:%d  (/state /plan /shot /key /call)"
          % port)
    return port
