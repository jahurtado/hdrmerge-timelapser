#!/usr/bin/env python3
"""Look at the plan and correct it.

The detector is right about most brackets and wrong about a few, and the few are
predictable: a burst whose bright flank sits on black and drags a noisy layer
across the disc, a truncated ladder better left out than rebuilt, a frame where
the radius wants to be smaller than the measurement says. Those are worth a
person's thirty seconds each, not another heuristic.

Everything the window changes is a field of the plan, and every change is marked
`manual`, so detecting again keeps it. Nothing here merges or writes an image:
the document is the only output, exactly as `apply` is the only thing that
writes pixels.

    up/down       the frame              del     leave this frame out
    1-9           a step in or out       r       cycle the blur radius
    + - 0         zoom in, out, fit
    ctrl+s        save the plan          ctrl+z  undo the last change

Qt is imported inside the functions that need it, so `detect` and `apply` run on
a machine with no toolkit installed.
"""

import json
import os
import sys

from . import __version__, plan, preview, rawmeta
from .detect import (RAW_EXTS, RADIUS_AUTO_MAX, RADIUS_BUDGET,
                     RADIUS_CANDIDATES, read_shot)

# Fallbacks. The real answers come from the package metadata through
# `cli.identity`, so they are written once, in `pyproject.toml`.
AUTHOR = "Jose A. Hurtado"
SITE = "elcacharrista.com"

# Tacked onto the homepage link so the site can tell the visit came from here,
# and which version. A desktop app opening a link sends no Referer -- there is
# no page it came from -- so the only place to say it is the address itself.
# Standard UTM keys, which is what analytics already reads, and the same shape
# `eclipse-aligner` uses.
CAME_FROM = "utm_source=hdrmerge-timelapser&utm_medium=app&utm_content=%s"


RECENT = 8

# The width of the frame list. One row is `◑ 214  19:29:55  4 steps · r250`
# plus its state, which is 40 characters of Menlo 11 -- so this is measured,
# not chosen, and it is why the column does not need to be adjustable.
LIST_WIDTH = 360
# How many merges to run at once in the first pass. HDRMerge is already
# threaded, so this is not about filling the machine; two is what kept the six
# performance cores busy without the machine going away.
# `eclipse-aligner`'s palette, kept identical rather than merely similar: the
# two windows are used in the same afternoon on the same frames, and a colour
# that means "measured" in one and nothing in the other is worse than no colour.
DETECTED = "#5ac8fa"      # what the program worked out by itself
HELD = "#ffb340"          # carried from elsewhere: a truncated ladder, a borrow
MANUAL = "#ff6ac1"        # what a person set
GONE = "#ff5f56"          # not measured, or deliberately left out
SUN = "#ffd400"           # the buffer-fired burst, the one anomaly worth marking
FAST = "#32d74b"          # a bracket fired back to back, with no dead time
REF = "#a78bfa"           # a reference: here, the burst a step was borrowed from

# One colour per exposure layer, most exposed first, and **chosen by
# measurement**: the first five are the set from the family's palette whose
# closest pair is furthest apart in Lab. The old order had sky, green, yellow,
# amber, pink, and yellow against amber came to a difference of 28 -- which is
# why two cards in the middle of a five-step bracket looked alike. These stay
# above 78 for any bracket up to five steps and above 45 at six.
#
# Red is also what the clipping view paints. The two never share a picture, and
# the alternatives that avoid the clash all cost more than they save.
LAYER_COLOURS = ((0, 229, 255), (0, 255, 136), (255, 212, 0),
                 (255, 95, 86), (167, 139, 250), (240, 240, 245))

VIEWS = ("the picture", "what clipped", "which layer, as merged",
         "which layer, after the blur", "this frame against the longest")

PREFETCH_JOBS = 2

# Workers taking from the editing queue. Two merges at once keep the machine
# busy without either of them crawling; more only makes each one slower.
MERGE_JOBS = 2


def _identity():
    from .cli import identity
    who, site = identity()
    # A frozen build may carry no package metadata to read, and a corner that
    # says "www." is worse than one that says nothing.
    return who or AUTHOR, site or SITE


def about_box(parent=None):
    """What this is, who wrote it, and where it lives.

    A free function and not a method: the opening window wants it too, and it
    has nothing to do with a document.
    """
    QtCore, _, QtWidgets = _widgets()
    who, site = _identity()
    b = QtWidgets.QMessageBox(parent)
    b.setTextFormat(QtCore.Qt.RichText)
    b.setText(
        '<div style="font-size:15px"><b>hdrmerge-timelapser</b> %s</div>'
        '<div style="color:#8a8a90; margin-top:6px">Detect the bracketed '
        'sets,<br>correct them, then merge.</div>'
        '<div style="margin-top:12px">%s<br>'
        '<a style="color:#5ac8fa" href="https://www.%s">www.%s</a></div>'
        % (__version__, who, site, site))
    b.setIconPixmap(app_icon().pixmap(QtCore.QSize(64, 64)))
    b.setStandardButtons(QtWidgets.QMessageBox.Ok)
    centre(b, parent)
    b.exec()


def signature(parent=None):
    """The name, the version and the address, as one clickable line.

    Both windows carry it, in the same corner and the same words, because it
    is the same claim about the same program. Built here rather than twice so
    they cannot drift apart -- and so the address only has to be got right
    once.

    Two links in one line: the name opens About, the address opens the
    browser. Handled by hand rather than by `openExternalLinks`, which would
    try to open `#about` as a URL.

    The author is in the About box and not here: three things in a corner
    nobody is reading is two things too many, and the name is the one that
    identifies it.

    The address carries where the click came from. A desktop app opening a
    link sends no Referer header -- there is no page it came from -- so the
    only way the site can tell is in the URL itself. What is shown stays the
    plain address.
    """
    QtCore, QtGui, QtWidgets = _widgets()
    site = _identity()[1]
    lab = QtWidgets.QLabel()
    lab.setTextFormat(QtCore.Qt.RichText)
    lab.setCursor(QtCore.Qt.PointingHandCursor)
    lab.setContentsMargins(0, 0, 0, 0)
    lab.setToolTip(_tip("what this is, and where it lives"))
    lab.linkActivated.connect(
        lambda u: about_box(parent) if u == "#about"
        else QtGui.QDesktopServices.openUrl(QtCore.QUrl(u)))
    # Two links, so two colours: whichever one the pointer is on lights up and
    # the other does not. Lighting both from one placeholder would have the
    # address answering for the name, which is the sort of thing that makes a
    # person doubt where the click will go.
    html = (
        '<a href="#about" style="color:%%s; text-decoration:none">'
        '<b>hdrmerge-timelapser</b>'
        '<span style="color:#4a4c53"> %s</span></a>'
        '<span style="color:#4a4c53"> · </span>'
        '<a href="https://www.%s/?%s" '
        'style="color:%%s; text-decoration:none">www.%s</a>'
    ) % (__version__, site, CAME_FROM % __version__, site)
    DIM, LIT = ("#8a8a90", "#5a5a60"), ("#e8e8ea", "#9ee0ff")

    def paint(hovered=""):
        lab.setText(html % (LIT[0] if hovered == "#about" else DIM[0],
                            LIT[1] if hovered.startswith("http") else DIM[1]))
    paint()
    lab.linkHovered.connect(paint)
    return lab


# The command sheet writes its keys in the macOS glyphs -- ⌘ for the command
# key, ⇧ for shift -- because that is where the tool was born. On Windows and
# Linux there is no Command key, so ⌘ is written out as Ctrl; ⇧ stays, because
# the shift glyph is standard on every keyboard and needs no spelling out. The
# bindings are already portable: they are declared as `Ctrl+...`, which Qt binds
# to ⌘ on macOS on its own; only the *labels* are drawn by hand.
_MAC = sys.platform == "darwin"
# ⌘ is the only key with no equivalent on a PC, so it is the only one rewritten.
_MOD_NAME = {"⌘": "Ctrl"}


def _native_keys(text):
    """The macOS-only key glyph in `text` rewritten for this platform.

    A no-op on macOS. Elsewhere ⌘ becomes Ctrl, joined to what it modifies with
    the `+` those platforms write: `⌘A` reads `Ctrl+A`. ⇧ is left as it is.
    """
    if _MAC:
        return text
    for glyph, name in _MOD_NAME.items():
        text = text.replace(glyph, name + "+")
    return text


def _tip(text):
    """A tooltip Qt will wrap.

    A plain-text tooltip is laid out on one line however long it is, and some
    of these run three sentences: what appeared was a caption wider than the
    screen rather than a paragraph. Rich text wraps, so they are handed over as
    rich text, escaped, and given a width -- Qt's own idea of one is the length
    of the longest line it was given.
    """
    import html

    return ('<qt><div style="white-space:normal" width="380">%s</div></qt>'
            % html.escape(_native_keys(" ".join(str(text).split()))))


def _paras(*blocks):
    """A tooltip of paragraphs rather than one long line.

    `_tip` hands Qt one paragraph and a width, which is right for a sentence
    and wrong for the format list: those run to three sentences of different
    kinds -- what the file is, what it costs, what to set in the editor that
    opens it -- and as one block they came out as a ribbon nobody reads to the
    end. Narrower and broken up, the eye can find the line it came for.

    A block beginning with a word and a colon gets that word in the key
    colour, which is how "Interpret:" becomes findable without a heading.
    """
    import html

    out = []
    for block in blocks:
        text = html.escape(" ".join(str(block).split()))
        head, sep, rest = text.partition(": ")
        if sep and " " not in head:
            text = ('<b style="color:#c8c8cc">%s</b> %s' % (head + ":", rest))
        out.append('<div style="margin-bottom:7px">%s</div>' % text)
    return ('<qt><div style="white-space:normal" width="330">%s</div></qt>'
            % "".join(out))


def _widgets():
    from PySide6 import QtCore, QtGui, QtWidgets
    return QtCore, QtGui, QtWidgets


def _dark(app):
    """A dark window, because the subject is a bright disc on a black sky."""
    app.setApplicationName("hdrmerge-timelapser")
    QtCore, QtGui, _ = _widgets()
    app.setStyle("Fusion")
    p = QtGui.QPalette()
    bg, mid, fg = (QtGui.QColor("#1c1c1e"), QtGui.QColor("#2c2c2e"),
                   QtGui.QColor("#e8e8ea"))
    for role, colour in ((QtGui.QPalette.Window, bg),
                         (QtGui.QPalette.Base, QtGui.QColor("#141416")),
                         (QtGui.QPalette.AlternateBase, mid),
                         (QtGui.QPalette.Button, mid),
                         (QtGui.QPalette.Text, fg),
                         (QtGui.QPalette.WindowText, fg),
                         (QtGui.QPalette.ButtonText, fg),
                         (QtGui.QPalette.ToolTipBase, mid),
                         (QtGui.QPalette.ToolTipText, fg),
                         (QtGui.QPalette.Highlight, QtGui.QColor("#0a84ff")),
                         (QtGui.QPalette.HighlightedText, QtGui.QColor("#fff"))):
        p.setColor(role, colour)
    app.setPalette(p)


def app_icon():
    """The window's icon, every size drawn for its size.

    Handing Qt all of them rather than one to scale is the point of drawing
    them separately: it picks the one made for the slot it is filling, and the
    16 px one is a different drawing, not a shrunk 512.

    On macOS the *Dock* icon comes from an .app bundle, not from here, so run
    as a script it stays Python's. This governs the window and the dialogs.
    """
    QtCore, QtGui, _ = _widgets()
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")
    ic = QtGui.QIcon()
    for s in (16, 32, 64, 128, 256, 512):
        p = os.path.join(here, "icon-%d.png" % s)
        if os.path.exists(p):
            ic.addFile(p, QtCore.QSize(s, s))
    return ic


def _app():
    _, _, QtWidgets = _widgets()
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _dark(app)
    app.setWindowIcon(app_icon())
    return app


# ------------------------------------------------------------- recent list ---

def _store():
    QtCore, _, _ = _widgets()
    d = QtCore.QStandardPaths.writableLocation(
        QtCore.QStandardPaths.AppDataLocation)
    return os.path.join(d, "recent.json")


def recent():
    try:
        with open(_store()) as fh:
            return [p for p in json.load(fh) if os.path.isdir(p)][:RECENT]
    except (OSError, ValueError):
        return []


def remember(path):
    path = os.path.abspath(path)
    paths = [p for p in recent() if p != path]
    paths.insert(0, path)
    try:
        os.makedirs(os.path.dirname(_store()), exist_ok=True)
        with open(_store(), "w") as fh:
            json.dump(paths[:RECENT], fh, indent=1)
    except OSError:
        pass


def forget(path):
    """Take a folder off the recent list, and touch nothing else.

    The list is a convenience, so removing from it is a convenience too: no
    dialog, no confirmation, nothing on disk. What used to sit on that row was
    a *reset* -- plan and previews deleted -- one click away on the screen
    where you are only choosing a folder. That is a destructive action wearing
    the clothes of a tidy-up, and it stays in the editor, behind `Reset All`,
    where the folder is open and the decision is deliberate.
    """
    path = os.path.abspath(path)
    # Read first, then open for writing. `open(store, "w")` truncates the file
    # the moment it is evaluated, and `recent()` inside the same statement
    # then read back an empty one -- so removing one row removed all of them.
    keep = [p for p in recent() if p != path]
    try:
        with open(_store(), "w") as fh:
            json.dump(keep, fh, indent=1)
    except OSError:
        pass


# ------------------------------------------------------- where windows go ---

# The window store is a *convenience*, kept apart from the plan: nothing here
# changes what a merge produces, and a corrupt or absent file must cost
# nothing. Every read falls back to the built-in size, and every write that
# fails is dropped -- a program that cannot start because it could not
# remember how big it was last time is worse than one that opens at 1440x900.

def _sizes_store():
    QtCore, _, _ = _widgets()
    d = QtCore.QStandardPaths.writableLocation(
        QtCore.QStandardPaths.AppDataLocation)
    return os.path.join(d, "windows.json")


def _sizes():
    try:
        with open(_sizes_store()) as fh:
            got = json.load(fh)
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def sized(win, name, default):
    """Open a window at whatever size it was left at, if that still fits.

    Only the size is kept, never the position: a remembered position is a
    window that opens off the edge of a screen that is no longer there, and
    where it goes is answered better by `centre`. The size is clamped to the
    screen it is about to open on, so a window sized on a large display and
    reopened on a laptop is merely large, not unusable.
    """
    QtCore, QtGui, _ = _widgets()

    got = _sizes().get(name)
    w, h = (got if isinstance(got, list) and len(got) == 2 else default)
    screen = (QtGui.QGuiApplication.screenAt(QtGui.QCursor.pos())
              or QtGui.QGuiApplication.primaryScreen())
    if screen is not None:
        r = screen.availableGeometry()
        w, h = min(int(w), r.width()), min(int(h), r.height())
    try:
        win.resize(int(w), int(h))
    except (TypeError, ValueError):
        win.resize(*default)


def keep_size(win, name):
    """Write down the size a window was left at. Called on the way out."""
    if win is None or win.width() < 200 or win.height() < 150:
        return                       # minimised, or already torn down
    sizes = _sizes()
    sizes[name] = [win.width(), win.height()]
    try:
        os.makedirs(os.path.dirname(_sizes_store()), exist_ok=True)
        with open(_sizes_store(), "w") as fh:
            json.dump(sizes, fh, indent=1)
    except OSError:
        pass


# What a title bar adds to a window, learnt from the first one shown. Qt only
# knows it once the native window exists, and placing a window before it is
# shown is the only way to place it without a visible jump -- so the first
# window is measured and every window after it is placed right first time.
_FRAME = [0, 0]


def centre(win, on=None):
    """Put a window in the middle of the screen it is going to appear on.

    Every window in the program, and the small ones most of all: Qt places a
    dialog over its parent, which on a wide screen with the editor at one end
    leaves the question you have to answer off in a corner. Centred, the thing
    being asked is where the eye already is.

    The screen is the one the window belongs to when it has a parent, and
    otherwise the one holding the pointer -- on two displays that is the one
    the person is working on, which the primary screen need not be.

    It places twice, and that is not belt and braces. Before the window is
    shown Qt does not know the height of its title bar, and a dialog with a
    parent has not yet been moved over that parent by Qt itself: measured
    here, that is 14 px out for a plain window and 42 px for such a dialog.
    After it is shown the frame is real and the placing is exact. So: place
    from what is known, then correct on the next turn of the event loop --
    which for a modal dialog is the first turn of its own loop.
    """
    QtCore, QtGui, _ = _widgets()

    screen = None
    if on is not None:
        handle = on.window().windowHandle()
        screen = handle.screen() if handle is not None else None
    if screen is None:
        screen = (QtGui.QGuiApplication.screenAt(QtGui.QCursor.pos())
                  or QtGui.QGuiApplication.primaryScreen())
    if screen is None:
        return

    def place(learn=False):
        try:
            r = screen.availableGeometry()
            if win.isVisible():
                g = win.frameGeometry()
                if learn and g.height() > win.height():
                    _FRAME[:] = [g.width() - win.width(),
                                 g.height() - win.height()]
                w, h = g.width(), g.height()
            else:
                # A window nobody has resized still measures 640x480, the Qt
                # default; and one that was resized is still grown at show
                # time to whatever its layout needs, which is how the editor
                # asked for 1440 and opened at 1657.
                if not win.testAttribute(QtCore.Qt.WA_Resized):
                    win.adjustSize()
                size = win.size().expandedTo(win.minimumSizeHint())
                w = size.width() + _FRAME[0]
                h = size.height() + _FRAME[1]
            win.move(r.x() + (r.width() - w) // 2,
                     r.y() + (r.height() - h) // 2)
        except RuntimeError:
            pass                       # the window went away before its turn

    place()
    QtCore.QTimer.singleShot(0, lambda: place(learn=True))


def count_raws(path):
    """How many raws a folder holds, by name alone -- no EXIF, so it is instant."""
    try:
        return sum(1 for n in os.listdir(path)
                   if os.path.splitext(n)[1].lower() in RAW_EXTS)
    except OSError:
        return 0


def self_command():
    """How to start this program again, as a subprocess.

    From a checkout that is `python -m hdrmerge_timelapser`. **From the app it
    is the app**, because a frozen build's `sys.executable` is the bundle's
    own binary: handing that `-m hdrmerge_timelapser` makes it read the module
    name as the command, and argparse rejects it with exit 2 -- which is what
    Process did in every packaged build, at 100 % of a progress bar, having
    merged nothing.
    """
    import sys

    if not getattr(sys, "frozen", False):
        return [sys.executable, "-m", "hdrmerge_timelapser"]
    # Inside the app, through the second name the bundle carries for exactly
    # this: a process started from Contents/MacOS is *the app* as far as macOS
    # is concerned, and takes a Dock icon of its own for the length of the job.
    # The same binary reached as Contents/Resources/worker checks in as
    # background and takes none. Missing -- an older bundle -- the direct path
    # still works, and `apply` drops its own icon a second later.
    worker = os.path.normpath(
        os.path.join(os.path.dirname(sys.executable), os.pardir,
                     "Resources", "worker"))
    if sys.platform == "darwin" and os.path.exists(worker):
        return [worker]
    return [sys.executable]


def first_raw(path):
    """The first file a folder offers to the merge, by name. None if it has none."""
    try:
        for name in sorted(os.listdir(path)):
            if os.path.splitext(name)[1].lower() in RAW_EXTS:
                return os.path.join(path, name)
    except OSError:
        pass
    return None


def describe(path):
    """What to say about a folder without opening it.

    The plan if there is one, since that is the useful number -- how much of it
    is decided, and how much of that a person decided -- and a raw count if there
    is not.
    """
    doc_path = plan.path_for(path)
    if os.path.isfile(doc_path):
        try:
            doc = plan.load(doc_path)
            manual = sum(1 for f in doc["frames"] if f.get("manual"))
            frames = [f for f in doc["frames"] if f["include"]]
            return "%d sets planned%s" % (
                len(frames), " · %d by hand" % manual if manual else "")
        except (ValueError, OSError):
            pass
    n = count_raws(path)
    return "%d raws · not planned yet" % n if n else "no raws here"


def ask():
    """The window that opens when there is no folder to open yet."""
    QtCore, _, QtWidgets = _widgets()
    app = _app()
    chosen = []

    class Drop(QtWidgets.QFrame):
        def __init__(self):
            super().__init__()
            self.setAcceptDrops(True)
            self.setObjectName("drop")
            self.setMinimumHeight(150)

        def _folder(self, e):
            for u in e.mimeData().urls():
                if u.isLocalFile() and os.path.isdir(u.toLocalFile()):
                    return u.toLocalFile()
            return None

        def dragEnterEvent(self, e):
            if self._folder(e):
                self.setProperty("over", True)
                self.setStyleSheet("")
                e.acceptProposedAction()

        def dragLeaveEvent(self, e):
            self.setProperty("over", False)
            self.setStyleSheet("")

        def dropEvent(self, e):
            self.setProperty("over", False)
            self.setStyleSheet("")
            if self._folder(e):
                take(self._folder(e))

    def take(path):
        path = path.rstrip(os.sep) or os.sep
        if not count_raws(path):
            problem.setText("No raw frames in %s — the folder should hold the "
                            "raws themselves, one level, no subfolders."
                            % os.path.basename(path))
            problem.show()
            return
        # Counting is by extension, which is instant and enough for a list of
        # folders; deciding is not. `.tif` is an accepted extension because
        # some cameras write raws that way, so a folder of developed TIFFs
        # counts as raws and would be taken all the way to a merge that
        # refuses them. One file is opened here to tell the two apart -- the
        # first one, which is all it takes when a folder is a timelapse.
        first = first_raw(path)
        if first and not rawmeta.is_raw(first):
            problem.setText("%s holds developed pictures, not raws — the "
                            "merge chooses per pixel which exposure to take "
                            "each sensor site from, and a developed picture "
                            "has no sensor sites left."
                            % os.path.basename(path))
            problem.show()
            return
        chosen.append(path)
        win.close()

    win = QtWidgets.QWidget()
    win.setWindowTitle("hdrmerge-timelapser")
    win.setMinimumWidth(560)
    sized(win, "start", (640, 560))
    v = QtWidgets.QVBoxLayout(win)
    # The bottom margin is small because the signature sits on it, and a
    # signature belongs in the corner. The air it needs above it is its own
    # top margin, below, so it does not push that corner up. Both numbers are
    # `eclipse-aligner`'s: the two windows are meant to look like one program.
    v.setContentsMargins(30, 26, 30, 10)
    v.setSpacing(16)
    head = QtWidgets.QLabel(
        '<div style="font-size:20px"><b>hdrmerge-timelapser</b>'
        '<span style="color:#5a5a60"> %s</span></div>'
        '<div style="color:#8a8a90;margin-top:4px">Detect the bracketed '
        'sets, correct them, then merge.</div>'
        % __version__)
    head.setTextFormat(QtCore.Qt.RichText)
    badge = QtWidgets.QLabel()
    badge.setPixmap(app_icon().pixmap(QtCore.QSize(56, 56)))
    top = QtWidgets.QHBoxLayout()
    top.setSpacing(14)
    top.addWidget(badge, 0, QtCore.Qt.AlignTop)
    top.addWidget(head, 1)
    v.addLayout(top)

    drop = Drop()
    dv = QtWidgets.QVBoxLayout(drop)
    dv.addStretch(1)
    for text, style in (("Drop a timelapse folder here",
                         "color:#c8c8cc;font-size:15px;"),
                        ("every raw in one folder, no subfolders",
                         "color:#5a5a60;")):
        lab = QtWidgets.QLabel(text)
        lab.setAlignment(QtCore.Qt.AlignCenter)
        lab.setStyleSheet(style)
        dv.addWidget(lab)
    pick = QtWidgets.QPushButton("Choose a folder…")
    pick.clicked.connect(lambda: take(QtWidgets.QFileDialog.getExistingDirectory(
        win, "Open a timelapse — the folder of raw frames") or ""))
    row = QtWidgets.QHBoxLayout()
    row.addStretch(1)
    row.addWidget(pick)
    row.addStretch(1)
    dv.addLayout(row)
    dv.addStretch(1)
    v.addWidget(drop)

    problem = QtWidgets.QLabel()
    problem.setWordWrap(True)
    problem.setStyleSheet("color:#ff6b60;")
    problem.hide()
    v.addWidget(problem)

    seen = recent()
    if seen:
        lab = QtWidgets.QLabel("RECENT")
        lab.setStyleSheet("color:#5a5a60;font-size:9px;letter-spacing:1.4px;")
        v.addWidget(lab)
    for p in seen:
        row = QtWidgets.QFrame()
        row.setObjectName("recent")
        row.setCursor(QtCore.Qt.PointingHandCursor)
        rh = QtWidgets.QHBoxLayout(row)
        rh.setContentsMargins(10, 5, 10, 6)
        rv = QtWidgets.QVBoxLayout()
        rv.setSpacing(1)
        name = QtWidgets.QLabel(os.path.basename(p))
        name.setStyleSheet("color:#e8e8ea;font-weight:600;")
        where = QtWidgets.QLabel("%s  ·  %s" % (describe(p), os.path.dirname(p)))
        where.setStyleSheet("color:#70737c;font-size:11px;")
        rv.addWidget(name)
        rv.addWidget(where)
        rh.addLayout(rv, 1)
        drop = QtWidgets.QPushButton("remove")
        drop.setCursor(QtCore.Qt.PointingHandCursor)
        drop.setToolTip(_tip("Take this folder off the list. Nothing on disk "
                             "is touched -- not the raws, not the plan, not "
                             "the previews."))
        drop.setStyleSheet(
            "QPushButton{background:transparent;color:#5a5a60;border:0;"
            "padding:2px 6px;font-size:11px;}"
            "QPushButton:hover{color:#ff9f6b;}"
            "QPushButton:disabled{color:#4a4a50;}")
        drop.clicked.connect(
            lambda _c=False, q=p, r=row: (forget(q), r.hide()))
        rh.addWidget(drop, 0, QtCore.Qt.AlignTop)
        name.mousePressEvent = lambda _e, q=p: take(q)
        where.mousePressEvent = lambda _e, q=p: take(q)
        row.mousePressEvent = lambda _e, q=p: take(q)
        v.addWidget(row)

    foot = QtWidgets.QHBoxLayout()
    foot.setContentsMargins(0, 14, 0, 0)
    foot.addStretch(1)
    foot.addWidget(signature(win))
    v.addStretch(1)
    v.addLayout(foot)

    win.setStyleSheet(
        "#drop{border:1.5px dashed #3a3d45;border-radius:6px;background:#17181c;}"
        "#drop[over=\"true\"]{border-color:#ffd400;background:#1d1e23;}"
        "#recent{border-radius:4px;} #recent:hover{background:#22242a;}")
    centre(win)
    win.show()
    app.exec()
    keep_size(win, "start")
    return chosen[0] if chosen else None


_CANVAS = None


def _Parts(parent=None):
    """A row painter that keeps every label its own colour.

    A `QListWidgetItem` has one foreground and a row here carries four
    independent facts -- how tightly the bracket was fired, whether it is
    complete, whether a person corrected it, what the queue is doing with it.
    Painting the row in a single colour meant they took turns: on a real plan
    eighteen rows of twenty are pink for having been corrected, and the one
    that is short had nowhere to say so.

    So the row is drawn as a little rich-text document. The plain text stays
    in `DisplayRole` -- `reload_list` compares it to decide what changed, and
    it is what the row would be read out as -- and the marked-up version rides
    along beside it.
    """
    QtCore, QtGui, QtWidgets = _widgets()

    class Parts(QtWidgets.QStyledItemDelegate):
        def paint(self, painter, option, index):
            html = index.data(QtCore.Qt.UserRole + 1)
            if not html:
                return super().paint(painter, option, index)
            opt = QtWidgets.QStyleOptionViewItem(option)
            self.initStyleOption(opt, index)
            opt.text = ""
            style = (opt.widget.style() if opt.widget
                     else QtWidgets.QApplication.style())
            style.drawControl(QtWidgets.QStyle.CE_ItemViewItem, opt, painter,
                              opt.widget)
            doc = QtGui.QTextDocument()
            doc.setDefaultFont(opt.font)
            doc.setDocumentMargin(0)
            doc.setHtml(html)
            painter.save()
            painter.translate(
                opt.rect.left() + 7,
                opt.rect.top() + (opt.rect.height() - doc.size().height()) / 2)
            doc.drawContents(painter)
            painter.restore()

    return Parts(parent)


def _canvas():
    """The big view: a picture you can zoom into and drag around.

    A label with a scaled pixmap could not do either, and on this material the
    thing worth looking at is a hairline -- the limb, a seam between exposures,
    the speckle a wide radius drags across the disc -- which at fit-to-window is
    two pixels tall. Wheel zooms about the cursor, drag pans, double-click fits.

    It keeps `setPixmap`/`setText` so the rest of the window talks to it exactly
    as it did to the label, including `setPixmap(null)` meaning "clear".
    """
    global _CANVAS
    if _CANVAS is not None:
        return _CANVAS
    QtCore, QtGui, QtWidgets = _widgets()

    class Canvas(QtWidgets.QGraphicsView):
        MIN, MAX = 0.05, 8.0

        def __init__(self, parent=None):
            super().__init__(parent)
            self.setScene(QtWidgets.QGraphicsScene(self))
            self.item = None
            self.zoomed = False
            self.setDragMode(QtWidgets.QGraphicsView.ScrollHandDrag)
            self.setTransformationAnchor(
                QtWidgets.QGraphicsView.AnchorUnderMouse)
            self.setRenderHints(QtGui.QPainter.SmoothPixmapTransform
                                | QtGui.QPainter.Antialiasing)
            self.setFrameShape(QtWidgets.QFrame.NoFrame)
            self.setStyleSheet("background:#141416;border-radius:4px;")
            self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
            self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)

        def _clear(self):
            self.scene().clear()
            self.item = None
            self.resetTransform()
            self.zoomed = False

        # Shift and drag hands the movement to whoever set this, in scene
        # pixels. Nothing here knows what a bracket is; the editor does.
        dragger = None

        def mousePressEvent(self, event):
            QtCore = _widgets()[0]
            if (self.dragger and self.item is not None
                    and event.modifiers() & QtCore.Qt.ShiftModifier):
                self._from = self.mapToScene(event.position().toPoint())
                self.dragger(0.0, 0.0, "start")
                return event.accept()
            super().mousePressEvent(event)

        def mouseMoveEvent(self, event):
            QtCore = _widgets()[0]
            if getattr(self, "_from", None) is not None:
                now = self.mapToScene(event.position().toPoint())
                self.dragger(now.x() - self._from.x(),
                             now.y() - self._from.y(), "move")
                self._from = now
                return event.accept()
            super().mouseMoveEvent(event)

        def mouseReleaseEvent(self, event):
            if getattr(self, "_from", None) is not None:
                self._from = None
                self.dragger(0.0, 0.0, "end")
                return event.accept()
            super().mouseReleaseEvent(event)

        def setText(self, text):
            # Where you were looking is remembered across the message. A merge
            # puts "merging…" in here and the picture comes back a few seconds
            # later; clearing threw the transform away, so every re-merge --
            # and every step of a single frame -- dropped you back to fit.
            if self.item is not None and self.zoomed:
                self._was = (self.transform(),
                             self.mapToScene(self.viewport().rect().center()),
                             self.item.pixmap().size())
            self._clear()
            if not text:
                return
            note = self.scene().addText(text)
            note.setDefaultTextColor(QtGui.QColor("#8a8a90"))
            self.setSceneRect(note.boundingRect())

        def setPixmap(self, pix):
            if pix is None or pix.isNull():
                return self._clear()
            was = getattr(self, "_was", None)
            if self.item is None and was and was[2] == pix.size():
                self.scene().clear()
                self.item = self.scene().addPixmap(pix)
                self.setSceneRect(self.item.boundingRect())
                self.setTransform(was[0])
                self.centerOn(was[1])
                self.zoomed = True
                self._was = None
                return
            self._was = None
            if self.item is not None and self.item.pixmap().size() == pix.size():
                # Same frame, new pixels -- a re-merge, or the exposure slider
                # moving -- so replace what the item holds and touch nothing
                # else. Rebuilding the scene and restoring the transform by
                # hand worked, to within the rounding of one `centerOn`, and
                # dragging the slider applied that rounding thirty times a
                # second: the picture crept down the view.
                self.item.setPixmap(pix)
                return
            self.scene().clear()
            self.item = self.scene().addPixmap(pix)
            self.setSceneRect(self.item.boundingRect())
            self.fit()

        def fit(self):
            if self.item is not None:
                self.fitInView(self.item, QtCore.Qt.KeepAspectRatio)
                self.zoomed = False

        def scale_now(self):
            return self.transform().m11()

        def wheel(self, direction):
            """Zoom a notch, as the wheel does, for the keys and for testing."""
            if self.item is None:
                return
            step = 1.25 if direction > 0 else 1 / 1.25
            if not (self.MIN <= self.scale_now() * step <= self.MAX):
                return
            self.scale(step, step)
            self.zoomed = True

        def wheelEvent(self, event):
            if self.item is None:
                return
            step = 1.25 if event.angleDelta().y() > 0 else 1 / 1.25
            now = self.scale_now()
            if not (self.MIN <= now * step <= self.MAX):
                return
            self.scale(step, step)
            self.zoomed = True

        def mouseDoubleClickEvent(self, _event):
            self.fit()

        def resizeEvent(self, event):
            super().resizeEvent(event)
            if not self.zoomed:
                self.fit()

    _CANVAS = Canvas
    return _CANVAS


# ----------------------------------------------------------------- editor ---

class Editor:
    """The plan editor. Attributes are the state `remote` reads and drives."""

    def __init__(self, folder, doc, path):
        QtCore, QtGui, QtWidgets = _widgets()
        self.folder = folder
        self.doc = doc
        self.path = path
        self.i = 0
        self.dirty = False
        self.undo = []
        self.redo = []
        self.msg = ""
        self.cam2out = None
        self.slots = {}
        self.generation = 0
        self.merged = None            # (anchor, steps, radius) now on screen
        self.merge_gen = 0            # so a superseded merge is dropped
        self.queue = []               # anchors waiting, in the order asked for
        self.running = {}             # anchor -> the state being merged now
        self.started = {}             # anchor -> when its merge began
        self.lin = None               # the linear preview now on screen
        self.levels = {}              # where clipping and the noise floor are
        self.ev = 0.0                 # what the exposure slider says
        self.compress = 0.0           # how flat the base of the picture is
        self.detail = 0.0             # how much local structure is amplified
        self.restart = False          # set by `Reset All`, read by `edit`
        self.view = 0                 # which of VIEWS is on screen
        self.overlay = "edges"        # or "layers", the two overlays
        self.range_text = ""          # the measured span, for the read line
        # Filtered from the start. On the real run 239 of 259 frames are single
        # exposures with nothing to decide -- no steps to weigh, no radius, no
        # merge -- and scrolling past them to reach the twenty that matter is
        # noise. They are still in the plan and still developed; this is only
        # about what you have to look through.
        self.only_merges = True
        self.rows = []                # which frame each visible row is
        self._spans = {}              # anchor -> its bracket, read once
        self.aim = None               # which step the nudging keys move
        self.aligning = False         # lining the bracket up, a mode of its own
        self.was = None               # the frame as it was when that began
        self.hidden = set()           # frames off the overlay, only in that mode
        self.msg_text = ""
        self.is_merge = False         # what is loaded: a merge or one frame
        self.mask = None              # HDRMerge's layer map for this merge
        self.blurred = None           # that map with the radius applied
        # Merges get their own workers. Sharing the global pool with the
        # thumbnails meant two merges could hold every slot for twenty seconds
        # and the step pictures beside them stayed black the whole time.
        self.merge_pool = QtCore.QThreadPool()
        self.merge_pool.setMaxThreadCount(MERGE_JOBS)

        class Signals(QtCore.QObject):
            done = QtCore.Signal(str, int)
            failed = QtCore.Signal(str)
            merged = QtCore.Signal(str, str, float)
            merge_failed = QtCore.Signal(str, str)

        self.signals = Signals()
        self.signals.done.connect(self.place)
        self.signals.failed.connect(self.note)
        self.signals.merge_failed.connect(self._merge_failed)
        self.signals.merged.connect(self._merge_arrived)
        self.wb = doc["settings"].get("exr_wb")
        self.sources = {n: os.path.join(folder, n)
                        for n in sorted(os.listdir(folder))
                        if os.path.splitext(n)[1].lower() in RAW_EXTS}

        self.win = QtWidgets.QWidget()
        self.win.setWindowTitle("hdrmerge-timelapser — %s"
                                % os.path.basename(folder))
        sized(self.win, "editor", (1440, 900))
        # The sibling's chrome, to the pixel: the two strips that frame the
        # picture share a background and a hairline, and whichever panel has
        # the keyboard wears a blue rim. A mode you cannot see is a mode you
        # will blame the program for.
        self.win.setStyleSheet(
            "#readline{background:#1a1b20;border-bottom:1px solid #26282e;}"
            "#commands{background:#1a1b20;border-top:1px solid #26282e;}"
            "QGraphicsView{border:2px solid transparent;}"
            "QGraphicsView:focus{border:2px solid #0a84ff;}"
            "QListWidget{border:2px solid transparent;}"
            "QListWidget:focus{border:2px solid #0a84ff;}")
        # No margin on the window itself: the read line and the command sheet
        # are *bars*, and a bar that stops short of the edge is a panel. They
        # run corner to corner as they do in `eclipse-aligner`, which is what
        # puts the signature in the bottom right corner instead of near it.
        # Everything between them carries its own margin.
        root = QtWidgets.QVBoxLayout(self.win)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.head = QtWidgets.QLabel()
        self.head.setTextFormat(QtCore.Qt.RichText)
        self.head.setContentsMargins(14, 12, 14, 10)
        root.addWidget(self.head)

        if doc["warnings"]:
            # One ⚠ per warning, not one for the lot: two of them joined by a
            # gap read as a single long sentence, and the second warning's
            # first words looked like the end of the first one's.
            warn = QtWidgets.QLabel("  ⚠  " + "     ⚠  ".join(
                w["says"] for w in doc["warnings"]))
            warn.setWordWrap(True)
            warn.setStyleSheet("color:#ffd400;background:#2a2611;"
                               "border-radius:4px;padding:6px;")
            holder = QtWidgets.QWidget()
            hold = QtWidgets.QHBoxLayout(holder)
            hold.setContentsMargins(14, 0, 14, 10)
            hold.addWidget(warn)
            root.addWidget(holder)

        # What you read goes above the picture; what you operate goes below
        # it. That is `eclipse-aligner`'s rule and it is the reason the window
        # was rearranged: the settings used to sit in a third column, so
        # reading the frame and changing it meant crossing the picture with
        # your eyes and then with the pointer.
        self.read_left = QtWidgets.QLabel()
        self.read_left.setTextFormat(QtCore.Qt.RichText)
        self.read_right = QtWidgets.QLabel()
        self.read_right.setTextFormat(QtCore.Qt.RichText)
        read = QtWidgets.QWidget()
        read.setObjectName("readline")
        rd = QtWidgets.QHBoxLayout(read)
        rd.setContentsMargins(12, 5, 12, 5)
        rd.addWidget(self.read_left)
        rd.addStretch(1)
        rd.addWidget(self.read_right)
        root.addWidget(read)

        # A fixed column, not a splitter. It was draggable for a while and the
        # handle was one more thing on screen to decide about: the list holds
        # one line per frame and that line has a known length, so the width it
        # wants is not a matter of opinion. The picture takes everything else,
        # which is the only measurement here that benefits from a bigger window.
        body = QtWidgets.QHBoxLayout()
        body.setContentsMargins(14, 10, 14, 10)
        body.setSpacing(12)

        left_panel = QtWidgets.QWidget()
        left = QtWidgets.QVBoxLayout(left_panel)
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(8)

        self.image = _canvas()()
        self.image.setMinimumSize(320, 240)
        self.image.dragger = self._drag_step
        left.addWidget(self.image, 1)

        # A word floating on the picture, not in a bar at the bottom: the eye
        # is on the limb and that is where a surprise has to be answered.
        self.notice = QtWidgets.QLabel(self.image)
        self.notice.setStyleSheet(
            "background:rgba(20,20,24,220);color:#e8e8ea;border-radius:5px;"
            "padding:7px 12px;")
        self.notice.setTextFormat(QtCore.Qt.RichText)
        self.notice.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents)
        self.notice.hide()
        self._notice_timer = QtCore.QTimer(self.win)
        self._notice_timer.setSingleShot(True)
        self._notice_timer.timeout.connect(self.notice.hide)

        strip = QtWidgets.QWidget()
        strip.setFixedHeight(96)
        self.steps_box = QtWidgets.QHBoxLayout(strip)
        self.steps_box.setContentsMargins(0, 0, 0, 0)
        self.steps_box.setSpacing(8)
        left.addWidget(strip)

        # Three knobs on how the picture is *shown*, and one on how it is
        # *made*, kept on separate rows for that reason: the first three never
        # touch a pixel of the merge, the plan or the EXR.
        tone = QtWidgets.QHBoxLayout()
        tone.setSpacing(8)
        self.slider, self.evlab = self._knob(
            tone, "exposure", -80, 80, 0, 190, self.set_exposure, "0.0 EV")
        tone.addSpacing(18)
        self.detail_slider, self.detaillab = self._knob(
            tone, "local contrast", 0, 100, 0, 200, self.set_detail, "0 %")
        reset = self._lab("reset", "#5a5a60", 11)
        reset.setCursor(QtCore.Qt.PointingHandCursor)
        reset.setToolTip(_tip("Back to plain exposure, no compression"))
        reset.mousePressEvent = lambda _e: self.reset_tone()
        tone.addWidget(reset)
        tone.addStretch(1)
        left.addLayout(tone)

        tools = QtWidgets.QHBoxLayout()
        tools.setSpacing(10)
        tools.addWidget(self._lab("blur radius", "#8a8a90"))
        self.radius = QtWidgets.QComboBox()
        self.radius.addItems(["auto"] + [str(r) for r in RADIUS_CANDIDATES])
        self.radius.setFixedWidth(140)
        self.radius.activated.connect(self.set_radius)
        tools.addWidget(self.radius)
        tools.addStretch(1)
        left.addLayout(tools)

        self.why = self._lab("", "#5a5a60", 11)
        self.why.setWordWrap(True)
        left.addWidget(self.why)
        body.addWidget(left_panel, 1)

        right = QtWidgets.QWidget()
        right.setFixedWidth(LIST_WIDTH)
        rv = QtWidgets.QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(6)
        self.singles = QtWidgets.QCheckBox("only the sets with a bracket")
        self.singles.setChecked(True)
        self.singles.setToolTip(_tip(
            "A set of one frame has nothing to decide -- nothing to weigh "
            "against it, no radius, no merge -- and on this material they are "
            "most of the folder. Hidden by default; they are still in the plan "
            "and still developed."))
        self.singles.clicked.connect(self.toggle_singles)
        rv.addWidget(self.singles)

        self.list = QtWidgets.QListWidget()
        self.list.setItemDelegate(_Parts(self.list))
        self.list.setUniformItemSizes(True)
        self.list.setTextElideMode(QtCore.Qt.ElideRight)
        self.list.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.list.setStyleSheet(
            "QListWidget{background:#141416;border:0;}"
            "QListWidget::item{padding:4px 7px;border-bottom:1px solid #202024;}"
            "QListWidget::item:selected{background:#0a3a66;}")
        # Several frames at once, for the two commands where that means
        # something. Everything else stays on the frame being shown: a bracket
        # is lined up one at a time, and "leave this out" is a decision made
        # looking at it.
        self.list.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.list.currentRowChanged.connect(self._row_picked)
        self.list.itemSelectionChanged.connect(self._read)
        rv.addWidget(self.list, 1)
        body.addWidget(right)
        root.addLayout(body, 1)
        # Two sheets, one visible: the keys mean different things while a
        # bracket is being lined up, and a sheet listing commands that will
        # refuse is worse than no sheet at all.
        self.sheets = QtWidgets.QStackedWidget()
        self.sheets.addWidget(self._commands())
        self.sheets.addWidget(self._align_sheet())
        self.sheets.setFixedHeight(self.sheets.widget(0).sizeHint().height())
        root.addWidget(self.sheets)

        # A key earns its place by being used often or by being the only way
        # in. Everything else lives in the sheet below as a label you click:
        # nineteen shortcuts was a list nobody could hold, and half of them
        # answered questions the window had stopped asking.
        keys = [("Up", lambda: self._arrow(0, -1)),
                ("Down", lambda: self._arrow(0, 1)),
                ("Left", lambda: self._arrow(-1, 0)),
                ("Right", lambda: self._arrow(1, 0)),
                ("N", lambda: self.go(1)), ("P", lambda: self.go(-1)),
                ("A", self.enter_align),
                ("Esc", self.cancel_align),
                ("Return", self.apply_align), ("Enter", self.apply_align),
                ("X", self._blend),
                ("Shift+K", lambda: (self.reset_offsets() if self.aligning
                                     else self.revert())),
                ("K", lambda: self.reset_offset_one() if self.aligning else None),
                ("R", lambda: self.cycle_radius(1)),
                ("Shift+R", lambda: self.cycle_radius(-1)),
                ("[", lambda: self.nudge_exposure(-5)),
                ("]", lambda: self.nudge_exposure(5)),
                ("F", self.zoom_fit),
                ("Del", self.toggle_include),
                ("Ctrl+Z", self.undo_last),
                ("Ctrl+Shift+Z", self.redo_last),
                ("Ctrl+S", self.save), ("Ctrl+R", self.process),
                ("Ctrl+D", self.autodetect), ("Ctrl+A", self.select_all)]
        for key, fn in keys:
            act = QtGui.QShortcut(QtGui.QKeySequence(key), self.win)
            act.activated.connect(fn)
        for n in range(1, 10):
            act = QtGui.QShortcut(QtGui.QKeySequence(str(n)), self.win)
            act.activated.connect(lambda k=n: self._number(k - 1))


        # The picture is what the window is for, so it is what the window
        # shows: the masks are held down and let go, not switched on and left
        # on. A mode you have to remember to leave is a mode that eventually
        # lies to you -- you come back from lunch looking at a layer map and
        # read it as the merge.
        class Momentary(QtCore.QObject):
            def eventFilter(self, _obj, event):
                if event.type() == QtCore.QEvent.KeyPress:
                    if not event.isAutoRepeat():
                        if editor._shifted_digit(event):
                            return True
                        editor._hold(event.key(), event.modifiers())
                elif event.type() == QtCore.QEvent.KeyRelease:
                    if not event.isAutoRepeat():
                        editor._let_go(event.key())
                return False

        editor = self
        self._momentary = Momentary()
        QtWidgets.QApplication.instance().installEventFilter(self._momentary)

        if plan.stale(doc, folder, RAW_EXTS):
            self.stale = QtWidgets.QLabel(
                "  ⟳  This folder has changed since the plan was measured. "
                "Measuring again keeps everything you set by hand.")
            self.stale.setStyleSheet("color:#7ec8ff;background:#12222e;"
                                     "border-radius:4px;padding:6px;")
            again = QtWidgets.QPushButton("Measure again")
            again.clicked.connect(self._measure_again)
            row = QtWidgets.QHBoxLayout()
            row.addWidget(self.stale, 1)
            row.addWidget(again)
            root.insertLayout(1, row)

        self.tick = QtCore.QTimer(self.win)
        self.tick.timeout.connect(lambda: self._say_waiting())
        self.tick.start(1000)
        self.win.closeEvent = self._closing

        self.reload_list()
        self.show_frame(self.rows[0] if self.rows else 0)

    # -- small helpers ----------------------------------------------------
    def _lab(self, text, colour="#e8e8ea", size=None, bold=False):
        _, _, QtWidgets = _widgets()
        w = QtWidgets.QLabel(text)
        style = "color:%s;" % colour
        if size:
            style += "font-size:%dpx;" % size
        if bold:
            style += "font-weight:600;"
        w.setStyleSheet(style)
        return w

    # Where the radius in force came from. Three answers and not two: most
    # folders are opened at one fixed radius, so "not auto" no longer means
    # "somebody set this bracket".
    WHOSE = {"manual": "yours", "fixed": "the folder's setting",
             "auto": "measured"}

    def auto_radius(self, f):
        """What the measurement would choose for this bracketed set, or None.

        Not the same as the radius in force: a folder opened at a fixed radius
        applies that one to every set and still scores them all, which is what
        makes `back to auto` and the line under the control possible.

        One implementation, in `detect`, because the window and the plan used
        to disagree about what the word meant: `choose_radius` walked up and
        stopped at the first candidate over budget, and here it took the
        widest under it. On a bracket where 3 spoils 0.136 % and 50 spoils
        0.000 % -- the score is not monotonic -- one said 3 and the other said
        250, about the same set, on the same screen.
        """
        from .detect import auto_radius

        info = f.get("blend_radius") or {}
        scores = {int(k): v for k, v in (info.get("candidates") or {}).items()
                  if v is not None}
        return auto_radius(scores, info.get("cap") or RADIUS_AUTO_MAX)

    def _show_why(self):
        """Put the reasoning under the controls, for the radius in force now."""
        self.why.setText(self._why(self.frame.get("blend_radius") or {}))

    def _why(self, info):
        """Why the radius is what it is, in the numbers detect measured.

        The line states what the radius in force costs and what auto would
        have done, and nothing else. It used to talk to the reader about it --
        "50 is yours", "wider is yours to pick" -- which is a caption
        explaining that a control is a control.

        Two things it got wrong, both visible on a frame where the crescent is
        bright and small. It claimed "the widest that spoils under 0.1 %%"
        while quoting 0.116 %%: when even the narrowest candidate is over
        budget there is no radius that satisfies the rule, and `choose_radius`
        keeps the least bad one, which is a different sentence. And it quoted
        radii from a ladder that no longer exists -- a plan measured by an
        older build scored 3, 30, 100 and 300, and asking for "the next one
        wider than 3" walked past four unmeasured candidates to answer 100. It
        only ever quotes radii this frame has a number for.
        """
        scores = {int(k): v for k, v in (info.get("candidates") or {}).items()
                  if v is not None}
        chosen = info.get("chosen")
        # Anything but auto is a number somebody chose -- for this bracket, or
        # for the whole folder when it was opened -- and the line has to start
        # from what is in force. Reading only "manual" here let a fixed-radius
        # folder explain auto's choice as though it were applied.
        mode = info.get("mode", "auto")
        if not scores:
            return ("%s, %s; this bracket has no measurements to compare it "
                    "against." % (chosen, self.WHOSE[mode])
                    if mode != "auto" else "")
        budget = 100 * RADIUS_BUDGET
        cap = info.get("cap") or RADIUS_AUTO_MAX
        under = [r for r in sorted(scores) if scores[r] <= budget and r <= cap]
        auto = max(under) if under else min(scores)
        if not under:
            why = ("no radius here stays under %g %% of the picture; auto takes "
                   "the narrowest, %s, at %.3f %%"
                   % (budget, auto, scores[auto]))
        else:
            why = ("auto takes %s, the widest that spoils under %g %% of the "
                   "picture, at %.3f %%" % (auto, budget, scores[auto]))
            wider = [r for r in sorted(scores) if r > auto]
            if auto == cap and cap < RADIUS_AUTO_MAX:
                # The reason it stopped there is the bracket, not the pixels.
                why += ("; %s is as wide as auto goes on steps of this size"
                        % cap)
            elif wider:
                why += "; %s would spoil %.3f %%" % (wider[0], scores[wider[0]])
            elif auto == RADIUS_AUTO_MAX:
                why += "; %s is the widest auto considers" % RADIUS_AUTO_MAX
        if mode == "auto":
            return why[0].upper() + why[1:] + "."
        mine = scores.get(chosen)
        return ("%s, %s, spoils %.3f %% of the picture. %s."
                % (chosen, self.WHOSE[mode], mine, why[0].upper() + why[1:])
                if mine is not None else
                "%s, %s, was not among the radii measured here. %s."
                % (chosen, self.WHOSE[mode], why[0].upper() + why[1:]))

    def _exposure(self, shot):
        """A step written the way the camera was set: shutter and ISO.

        The shutter alone was ambiguous on this material and in the worst
        place: the eclipse brackets change ISO between clips -- 100 either side
        of totality, 640 through it -- so two steps reading `1/125` can be a
        stop and a half apart, and a bracket that crosses the boundary is the
        one thing that makes HDRMerge flicker. It has to be on screen.
        """
        from .detect import shutter

        if not shot:
            return ""
        when = shot.time.strftime("%H:%M:%S") if shot.time else ""
        return "%s · ISO %s%s" % (shutter(shot.exp), shot.iso or "?",
                                  " · " + when if when else "")

    def _offset(self, step):
        """An offset -- a step's or the whole frame's -- written plainly.

        Both keep it under the same key and mean the same thing by it, so one
        reader serves both: whole numbers as whole numbers, quarters as
        quarters.
        """
        dx, dy = step.get("offset") or (0, 0)
        if dx == int(dx) and dy == int(dy):
            return "%+d %+d px" % (dx, dy)
        return "%+.2f %+.2f px" % (dx, dy)

    def _borrowed(self, f):
        """The steps of this frame that came from another burst.

        There was an `allow borrowing` switch here and it is gone. It said
        nothing about where the steps came from or whether any were in use, and
        once the steps became clickable it did exactly what unticking those two
        rows does -- the same decision under a vaguer name. What is worth
        keeping is not the switch but the fact, so each borrowed step says on
        its own row which burst lent it and how far away that burst was.
        """
        return set((f.get("borrow") or {}).get("steps") or [])

    COMMANDS = (
        ("VIEW", (
            ("hold c", "show clipped", None,
             "While held: every pixel that reached the sensor's white level, "
             "in red. That is what the merge could not measure."),
            ("hold l", "show layers mask (blurred)", None,
             "While held: which exposure each pixel is taken from, after the "
             "blur radius has spread the boundaries. This is the map the "
             "merge actually uses."),
            ("hold ⇧L", "show layers mask (original)", None,
             "The same map as HDRMerge builds it, before the blur radius "
             "touches it."),
            ("f", "fit", "zoom_fit",
             "Fit the picture to the window. The wheel zooms about the "
             "pointer and dragging moves it."))),
        ("EDIT", (
            ("n p", "skipping deleted", None,
             "The next or previous set, stepping over the ones you have "
             "skipped. The arrows walk the list itself."),
            ("⇧1–⇧9", "disable / enable frame", None,
             "Put one frame of the bracket into the merge or take it out. One "
             "that resolves nothing adds no range and costs noise."),
            ("r ⇧R", "increase / decrease blur radius", "cycle_radius",
             "How far HDRMerge spreads the boundary between exposures. Wide "
             "removes the halo where the short steps carry signal and drags "
             "noise across the disc where they do not."),
            ("a", "frame alignment", "align_mode",
             "Align the frames of this bracket against each other. Nothing "
             "merges while it is on."),
            ("del", "skip this bracketed set", "toggle_include",
             "Leave this bracketed set out of the sequence. Press again to "
             "bring it back; the list dims what is skipped."),
            ("⇧K", "reset this bracketed set", "revert",
             "Undo every hand change on this set -- frames, radius, "
             "offsets, exclusion -- and put back what detect measured."))),
        ("PROJECT", (
            ("⌘S", "save", "save",
             "Write the plan. The merges are cached files and survive on "
             "their own; the decisions only survive if they are saved."),
            ("⌘Z ⇧⌘Z", "undo / redo", "undo_last",
             "Step back and forward through the changes made by hand."),
            ("⌘D", "autodetect HDR settings", "autodetect",
             "Applies what the measurement would have chosen -- the blur "
             "radius, and which frames are worth merging -- to the sets "
             "picked in the list -- ⌘A selects them all. Everything was "
             "scored when the folder was measured, so it costs nothing to run "
             "and nothing to undo. Your alignment and your exclusions are "
             "left alone."),
            ("⌘R", "process…", "process",
             "Merge and develop the whole plan at full resolution. Minutes "
             "to hours; the window stays open."),
            ("", "Reset All", "start_over",
             "Resets the whole folder: the plan and every preview go, and it "
             "is measured again from nothing. The raw frames are never "
             "touched."))),
    )

    ALIGN_COMMANDS = (
        ("VIEW", (
            ("hold c", "show clipped", None,
             "The same as outside the mode: what reached the white level."),
            ("hold l", "show mask", None,
             "Which exposure each pixel is taken from, after the blur radius "
             "has spread the boundaries."),
            ("hold ⇧L", "show original mask (no blur)", None,
             "The same map as HDRMerge builds it, before the radius touches "
             "it."),
            ("x", "edges or frames", None,
             "Switch between the two overlays: the edges of each frame, which "
             "is what you line up, and the frames themselves, each one drawn "
             "in its own colour. Edges keep only the strongest 4 % of the "
             "picture, so the frames are worth a look to tell a limb that is "
             "really doubled from grain that got through."),
            ("f", "fit", "zoom_fit",
             "Fit the picture to the window."))),
        ("EDIT", (
            ("← ↑ ↓ →  ⇧ drag", "move selected frame", None,
             "Two pixels a press -- one Bayer cell, which is exact because it "
             "lands on whole photosites. The mouse moves in the same step: "
             "anything finer has to interpolate inside one colour of the "
             "mosaic, and that costs sharpness for a correction this frame "
             "does not need."),
            ("1–9", "select frame", None,
             "Which frame of the bracket the arrows move. The selected one is "
             "drawn on top of the overlay and outlined below."),
            ("⇧1–9", "show / hide frame", None,
             "A frame out of the merge is one off the overlay."),
            ("k", "reset this frame", "reset_offset_one",
             "Puts the selected frame back where the camera took it, and "
             "leaves the rest of the set where you moved them."),
            ("⇧K", "reset the whole set", "reset_offsets",
             "Clears every offset in this bracketed set, leaving all its "
             "frames where the camera put them -- not only the moves made "
             "since you came in. It touches nothing else: not the radius, "
             "not which frames are in."),
            ("⌘Z", "undo the last move", "undo_last",
             "A whole drag counts as one move."))),
        ("ALIGNMENT", (
            ("↩", "apply alignment", "apply_align",
             "Leave the mode, keeping the offsets, and merge the frame once "
             "with them. Return and escape are the pair a dialog has taught "
             "everybody, and they are the only way out: a goes in, and one "
             "key that both starts and ends a job is a key you press twice "
             "by accident."),
            ("esc", "cancel alignment", "cancel_align",
             "Leave the mode and put every offset back as it was when you "
             "came in. What was aligned before is left alone."),
            ("", "measure them all", "align_steps",
             "Phase-correlate every frame against the longest and move them "
             "there. One the measurement cannot trust is left where it is "
             "and says so."))),
    )

    def _align_sheet(self):
        """The bottom strip as it reads while lining a bracket up."""
        return self._sheet(self.ALIGN_COMMANDS,
                           "aligning — nothing merges until ↩, esc puts it back")

    def _commands(self):
        """The command sheet along the bottom, and the message in its gap.

        It replaces three buttons that named themselves and taught nothing: the
        keys were real and invisible, living in `--help` where nobody looks
        while working. Grouped by scope -- what you see, where you are, this
        frame, everything, this folder -- and every label is clickable, so the
        rare things need no key at all: that is why `back to auto`, `measure
        again` and `Reset All` have none.
        """
        QtCore, QtGui, QtWidgets = _widgets()

        return self._sheet(self.COMMANDS)

    def _sheet(self, groups, note=None):
        """One strip of key-and-what columns, with the message in its gap."""
        QtCore, QtGui, QtWidgets = _widgets()

        panel = QtWidgets.QWidget()
        panel.setObjectName("commands")
        row = QtWidgets.QHBoxLayout(panel)
        row.setContentsMargins(12, 8, 12, 8)
        row.setSpacing(30)
        # Not reset here: this runs once per sheet, and starting over on the
        # second one threw away the first one's labels -- including the digits
        # that have to be renumbered per frame.
        self.keylabels = getattr(self, "keylabels", {})
        self.cmdrows = getattr(self, "cmdrows", {})
        for title, items in groups:
            # Each column as wide as its own longest line, measured rather
            # than guessed -- a fixed width fits one group and clips the next,
            # and `show layers mask (blurred)` is twice the length of `save`.
            # Fixed once at build time so the columns do not shuffle sideways
            # when a label changes with the frame.
            # The sheet is set in the window's own font at its own size, as
            # the sibling tool sets it: Menlo is for the list of shots, where
            # the columns have to line up, and using it here made a smaller,
            # narrower sheet that did not look like the same pair of tools.
            font = self._lab("").font()
            fm = QtGui.QFontMetrics(font)
            # Column by column, not row by row: the widest key and the widest
            # label are rarely the same row, and adding them per row left
            # `autodetect HDR settings` clipped by the length of `⌘D`.
            wide = (max(fm.horizontalAdvance(self._key_plain(k) or "·")
                        for k, _, _, _ in items)
                    + max(fm.horizontalAdvance(w) for _, w, _, _ in items))
            holder = QtWidgets.QWidget()
            holder.setFixedWidth(wide + 9 + 8)      # the grid's own spacing
            col = QtWidgets.QGridLayout(holder)
            col.setContentsMargins(0, 0, 0, 0)
            col.setVerticalSpacing(1)
            col.setHorizontalSpacing(9)
            col.addWidget(self._cap(title), 0, 0, 1, 2)
            for n, (key, what, method, tip) in enumerate(items):
                k = self._lab("")
                k.setTextFormat(QtCore.Qt.RichText)
                # Right-aligned so the *letter* falls on one vertical whatever
                # hangs in front of it: `f`, `⇧K` and `⌘D` all line up.
                k.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
                self._set_key(k, key)
                col.addWidget(k, n + 1, 0)
                self.keylabels[what] = k
                lab = self._lab(what, "#c8c8cc")
                k.setToolTip(_tip(tip))
                lab.setToolTip(_tip(tip))
                if method:
                    lab.setCursor(QtCore.Qt.PointingHandCursor)
                    lab.mousePressEvent = (
                        lambda _e, m=method, w=lab:
                        None if w.property("off") else getattr(self, m)())
                    # White under the pointer, so what can be clicked says so
                    # when you are over it and stays quiet the rest of the time.
                    lab.enterEvent = lambda _e, w=lab: w.setStyleSheet(
                        "color:#ffffff;")
                    lab.leaveEvent = lambda _e, w=lab: w.setStyleSheet(
                        "color:#c8c8cc;")
                col.addWidget(lab, n + 1, 1)
                self.cmdrows[what] = (k, lab, method, key)
            col.setColumnStretch(1, 1)
            row.addWidget(holder)
        row.addStretch(1)
        if note:
            row.addWidget(self._lab(note, MANUAL, 11))
            row.addSpacing(14)
        # A fixed room rather than a stretch. `Ignored` plus a stretch factor
        # is the combination that looks right and is not: Qt gives the widget
        # the stretch only when nothing else claims it, and every attempt at it
        # here ended with the message wrapped into a column three letters wide.
        # Both sheets carry one, and `note` writes to whichever is showing.
        msg = self._lab("", "#8a8a90")
        msg.setWordWrap(True)
        msg.setFixedWidth(240)
        msg.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignBottom)
        self.msgs = getattr(self, "msgs", [])
        self.msgs.append(msg)
        row.addWidget(msg, 0, QtCore.Qt.AlignBottom)
        row.addSpacing(18)
        # The name legible and the rest quiet: it is what identifies the
        # window, and at #3f4148 on this background it was not there at all.
        who = signature(self.win)
        row.addWidget(who, 0, QtCore.Qt.AlignBottom)
        return panel

    def _knob(self, into, name, lo, hi, start, width, on_change, first):
        """One labelled slider with its reading, since there are three of them."""
        _, _, QtWidgets = _widgets()
        QtCore = _widgets()[0]

        title = self._lab(name, "#8a8a90")
        tip = {"exposure": "How bright the picture is drawn, in stops. Also "
                           "[ and ] on the keyboard. Display only: it changes "
                           "no pixel of the merge, the plan or the EXR.",
               "local contrast": "Flattens the slow falloff and lifts the fine "
                                 "structure on top of it -- the streamers, the "
                                 "seam a bad merge leaves. Display only."}
        title.setToolTip(_tip(tip.get(name, "")))
        into.addWidget(title)
        bar = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        bar.setToolTip(_tip(tip.get(name, "")))
        bar.setRange(lo, hi)
        bar.setValue(start)
        bar.setFixedWidth(width)
        bar.valueChanged.connect(on_change)
        into.addWidget(bar)
        out = self._lab(first, "#e8e8ea", 11)
        out.setFixedWidth(52)
        into.addWidget(out)
        return bar, out

    def _floor(self):
        """The coverage under which detect calls a step empty, in percent."""
        return (self.doc.get("settings") or {}).get("min_step_coverage", 0.01)

    def _cap(self, text):
        """A column heading, letter-spaced the way `eclipse-aligner` sets it."""
        w = self._lab(text, "#5a5a60", 9)
        w.setStyleSheet("color:#5a5a60;font-size:9px;"
                        "letter-spacing:1.4px;padding-bottom:2px;")
        return w

    # Amber for the keys, and the modifier dimmer than the letter, because the
    # letter is what the eye is hunting for -- the same two colours the sibling
    # tool uses, so the two sheets read as one pair of hands.
    KEY_BRIGHT, KEY_DIM = "#ffd400", "#9a7a12"

    # Words in a key column that are not keys: they qualify the key beside
    # them, and painting them as bright as it makes the column unreadable.
    KEY_WORDS = ("hold", "drag")

    def _set_key(self, label, text):
        """Put a key on its label, painted."""
        label.setText(self._key_html(text) if text
                      else '<span style="color:#3f4148">·</span>')

    # ⌘ before ⇧, so a two-key run reads `Ctrl+⇧` and not `⇧Ctrl+`.
    _MOD_ORDER = {"⌘": 0, "⇧": 1}
    # How each modifier is written off macOS: the Command key becomes Ctrl and
    # carries the `+` that joins it to what follows; ⇧ stays the glyph it is.
    _WIN_MOD = {"⌘": "Ctrl+", "⇧": "⇧"}

    def _key_runs(self, word):
        """A key split into (text, dim) runs, written for this platform.

        Grouped on the original macOS glyphs so a modifier is dim wherever it
        falls -- `⇧1–⇧5` has one in the middle, and stripping only the leading
        one left it as bright as the digits. Off macOS ⌘ is rewritten as Ctrl+
        (there is no Command key on a PC) while ⇧ is kept, so `⌘S` reads
        `Ctrl+S`, `⇧⌘Z` reads `Ctrl+⇧Z`, and `⇧K` is unchanged.
        """
        if word in self.KEY_WORDS:
            return [(word, True)]
        runs, dim, run = [], None, ""
        for ch in word:
            now = ch in "⇧⌘"
            if now != dim and run:
                runs.append((run, dim))
                run = ""
            dim, run = now, run + ch
        if run:
            runs.append((run, dim))
        if _MAC:
            return runs
        out = []
        for text, is_mod in runs:
            if is_mod:
                text = "".join(self._WIN_MOD[c]
                               for c in sorted(text, key=self._MOD_ORDER.get))
            out.append((text, is_mod))
        return out

    def _key_html(self, text):
        """A key as the sheet paints it: letters bright, everything else dim."""
        out = []
        for word in text.split(" "):
            bit = ""
            for run, dim in self._key_runs(word):
                bit += '<span style="color:%s">%s</span>' % (
                    self.KEY_DIM if dim else self.KEY_BRIGHT, run)
            out.append(bit)
        return "&nbsp;".join(out)

    def _key_plain(self, text):
        """The same key as `_key_html` paints, but as plain text.

        What the column width is measured from, so it is measured from what the
        sheet actually shows -- `Ctrl+D` is wider than `⌘D`, and measuring the
        glyphs would clip the words the glyphs became.
        """
        return " ".join("".join(run for run, _ in self._key_runs(word))
                        for word in text.split(" "))

    @property
    def frame(self):
        return self.doc["frames"][self.i]

    SYNC_COLOUR = {"fast": FAST, "normal": "#8a8a90", "slow": GONE}

    def sync_of(self, f):
        """How tightly this bracket was fired: fast, normal, slow or None.

        A plan measured before this was recorded only knows the old `sync`
        kind, which meant the same thing with a cruder rule; it answers from
        that rather than saying nothing.
        """
        if f.get("sync"):
            return f["sync"]
        if not self.bracketed(f):
            return None
        return "fast" if f["kind"] == "sync" else "normal"

    def short_by(self, f):
        """How many frames this bracket is missing, or 0 if it is complete.

        Counted on the frames the camera shot, never on the borrowed ones: a
        lent frame fills the merge, not the bracket.
        """
        want = f.get("expected_frames") or self.doc.get("ladder_steps")
        own = f.get("own_frames")
        if own is None:
            lent = len((f.get("borrow") or {}).get("steps") or [])
            own = len(f["steps"]) - lent
        if not want or not self.bracketed(f) or own >= want:
            return 0
        return want - own

    def _what(self):
        """What this frame is, in one phrase: the middle of the read line."""
        f = self.frame
        steps = plan.merged_steps(f)
        radius = (f.get("blend_radius") or {}).get("chosen")
        if not steps:
            return "no frames chosen"
        if len(steps) < 2:
            path = self.sources.get(steps[0])
            return ("one frame, developed as it is · %s"
                    % self._exposure(read_shot(path)) if path else "one frame")
        return "%d frames at r%s" % (len(steps), radius or "—")

    def _read(self, middle=None):
        """The one line above the picture, and the state of the sheet with it.

        Everything you read, in order.

        **Which frame · what it is made of · what the numbers say**, and on the
        right the view mode *with the key that changes it*, because a mode you
        can read but cannot find the switch for is half an answer.
        """
        f = self.frame
        self._light_sheet(f)
        g = lambda c, t: '<span style="color:%s">%s</span>' % (c, t)
        bar = g("#3a3a42", "│")
        bits = [g("#8a8a90", "%d/%d" % (f["index"], len(self.doc["frames"]))),
                '<b style="color:#e8e8ea">%s</b>' % f["anchor"]]
        sync = self.sync_of(f)
        if sync:
            short = self.short_by(f)
            bits.append(g(self.SYNC_COLOUR[sync], "%s SYNC" % sync.upper()))
            bits.append(g(GONE if short else "#8a8a90",
                          "PARTIAL BRACKET" if short else "FULL BRACKET"))
        else:
            bits.append(g(DETECTED, "SINGLE"))
        if not f["include"]:
            bits.append(g("#ff453a", "LEFT OUT"))
        picked = len(self.chosen())
        if picked > 1:
            bits.append(g(MANUAL, "%d SELECTED" % picked))
        if f.get("offset") and any(f["offset"]):
            bits.append(g(MANUAL, "MOVED %s" % self._offset(f)))
        bits += [bar, g("#c8c8cc", middle or self._what())]
        if self.range_text:
            bits += [bar, g("#8a8a90", self.range_text)]
        self.read_left.setText("  ".join(bits))
        # Only while something is held: the read line says what you are
        # looking at, and says nothing when you are looking at the picture.
        # What the keys are is the sheet's job, once, at the bottom.
        if self.view == 4 and self.aim is not None:
            from .detect import shutter

            path = self.sources.get(f["steps"][self.aim]["frame"])
            which = shutter(read_shot(path).exp) if path else "this frame"
            self.read_right.setText('<b style="color:%s">MOVING %s</b>'
                                    % (MANUAL, which))
        else:
            self.read_right.setText(
                "" if not self.view else
                '<b style="color:%s">%s</b>' % (DETECTED,
                                                VIEWS[self.view].upper()))

    def note(self, text, flash=True):
        """Say something. In the gap of the command sheet, and over the picture.

        Both, because they answer different questions: the sheet keeps the last
        thing that happened for as long as you want to read it, and the notice
        catches the eye that is on the limb and not on the bottom of the window.
        """
        QtCore, _, _ = _widgets()

        self.msg_text = text
        for label in getattr(self, "msgs", []):
            label.setText(text)
        if flash and hasattr(self, "notice"):
            self.notice.setText(text)
            self.notice.adjustSize()
            r = self.image.rect()
            self.notice.move(max(8, (r.width() - self.notice.width()) // 2), 12)
            self.notice.show()
            self.notice.raise_()
            self._notice_timer.start(2600)

    # -- the list ---------------------------------------------------------
    def span(self, f):
        """The bracket's fastest and slowest step, as a photographer writes it.

        Read from the raws once per frame and kept: the plan records which
        files a merge is made of, not what they were shot at, and a list of 442
        frames would otherwise re-read every EXIF header on every refresh.
        """
        from .detect import shutter

        anchor = f["anchor"]
        if anchor in self._spans:
            return self._spans[anchor]
        exps = []
        for step in f["steps"]:
            if not step["use"]:
                continue
            path = self.sources.get(step["frame"])
            shot = read_shot(path) if path else None
            if shot and shot.exp:
                exps.append(shot.exp)
        out = ("%s–%s" % (shutter(min(exps)), shutter(max(exps)))
               if len(exps) > 1 else shutter(exps[0]) if exps else "")
        self._spans[anchor] = out
        return out

    # When each command has anything to act on, given the shot on screen.
    # A key that would do nothing is a key nobody should be invited to press,
    # and the message when it is pressed anyway says why.
    #
    # The two resets are the interesting ones: they are lit by whether there
    # is anything to undo, so an untouched shot shows them asleep. That is the
    # honest answer to "what would this do?" -- nothing -- and it doubles as a
    # reading of the shot: bright means you have been here.
    LIVE = {
        "disable / enable frame": lambda s, f: s.bracketed(f),
        "increase / decrease blur radius": lambda s, f: s.bracketed(f),
        "frame alignment": lambda s, f: s.bracketed(f),
        "reset this bracketed set": lambda s, f: bool(f.get("manual")),
        "reset the whole set": lambda s, f: any(
            any(step.get("offset") or ()) for step in f["steps"]),
        "reset this frame": lambda s, f: bool(
            s.aim is not None and s.aim < len(f["steps"])
            and any(f["steps"][s.aim].get("offset") or ())),
    }

    def _light_sheet(self, f):
        """Dim the commands this set has nothing for, on both sheets."""
        QtCore, _, _ = _widgets()

        lit = getattr(self, "_lit", None)
        if lit is None:
            lit = self._lit = {}
        for what, live in self.LIVE.items():
            row = getattr(self, "cmdrows", {}).get(what)
            if not row:
                continue
            on = bool(live(self, f))
            if lit.get(what) == on:
                continue                  # repainting an unchanged row is work
            lit[what] = on
            key, lab, method, was = row
            lab.setProperty("off", not on)
            # Only the name dims. The key stays amber, as it does next door:
            # the key column is a picture of the keyboard and the key does not
            # stop existing -- what changes is whether the command it names has
            # anything to act on. Taking the key away as well read as a
            # different sheet rather than the same one with a row asleep.
            lab.setStyleSheet("color:%s;" % ("#c8c8cc" if on else "#4a4c53"))
            self._set_key(key, was)
            lab.setCursor(QtCore.Qt.PointingHandCursor if (on and method)
                          else QtCore.Qt.ArrowCursor)


    def bracketed(self, f):
        """Whether this shot is one the editor has anything to decide about."""
        return len(f["steps"]) > 1

    def nothing_to_decide(self):
        """True, and says so, when this shot is a single frame.

        A shot with no bracket is developed exactly as it was taken: there is
        nothing to merge, so no frames to weigh against each other and no
        radius to blend them with. It was still taking those commands -- `r`
        set a blur radius on a shot that will never be merged and marked it
        corrected by hand, and `⇧1` took its only frame out of a merge that
        does not exist, which `apply` then ignored. An edit that appears to do
        something and does nothing is worse than a refusal.

        What stays is what is about the sequence rather than the merge: `del`
        leaves the shot out of it, and `⇧K` puts that back.
        """
        if self.bracketed(self.frame):
            return False
        self.note("a single frame: nothing to merge, so nothing to weigh "
                  "and no radius to set")
        return True

    def toggle_singles(self):
        """Show every frame of the plan, or only the ones with a bracket."""
        if self.busy_aligning():
            return self.singles.setChecked(self.only_merges)
        self.only_merges = not self.only_merges
        self.singles.setChecked(self.only_merges)
        hidden = sum(1 for f in self.doc["frames"] if not self.bracketed(f))
        self.note("showing %s"
                  % ("only the %d bracketed frames, %d single ones hidden"
                     % (len(self.doc["frames"]) - hidden, hidden)
                     if self.only_merges else "every frame of the plan"))
        self.reload_list()
        if self.i not in self.rows and self.rows:
            self.show_frame(self.rows[0])

    def go(self, step):
        """The next set down the list, skipping the ones already skipped.

        The arrows walk every row, this walks the ones still in the sequence --
        which is the difference worth having a second pair of keys for: once a
        frame is deleted you are done with it, but you still want to be able to
        land on it and bring it back.
        """
        if self.busy_aligning():
            return
        if not self.rows:
            return
        at = self.rows.index(self.i) if self.i in self.rows else 0
        for _ in range(len(self.rows)):
            at = (at + step) % len(self.rows)
            if self.doc["frames"][self.rows[at]]["include"]:
                return self.show_frame(self.rows[at])
        self.note("every set in the list is skipped")

    def reload_list(self):
        """Bring the list up to date **in place**, never by rebuilding it.

        It used to clear the whole list and add every row again on any change
        -- and a change includes clicking a row, since that starts a merge.
        `clear()` throws away the scroll position, so the list jumped back and
        then scrolled to the selection, and the row that had been under the
        pointer was no longer the frame you had just clicked: click, and the
        selection landed somewhere else. Rows are now edited where they are,
        and the current row is only set when it is actually wrong, because
        setting it scrolls.
        """
        QtCore, QtGui, QtWidgets = _widgets()

        rows = []
        self.rows = []
        for index, f in enumerate(self.doc["frames"]):
            if self.only_merges and not self.bracketed(f):
                continue
            self.rows.append(index)
            # The glyph carries how tightly the bracket was fired and the
            # count carries whether it is complete, so both survive a row that
            # is already coloured for something else -- and on a real plan
            # most rows are, since a corrected one is pink.
            sync = self.sync_of(f)
            mark = {"fast": "◆", "slow": "◇"}.get(sync, "●" if sync else "·")
            short = self.short_by(f)
            steps = len(plan.merged_steps(f))
            radius = (f.get("blend_radius") or {}).get("chosen")
            # Where this frame's picture is. A merge is up to 23 s, so a list
            # that looks the same before and after makes the wait feel like a
            # fault: a frame says whether it is queued, being merged, or simply
            # not made yet.
            where = self.queue_place(f)
            if where and where[0] == "merging":
                state = "  merging"
            elif where:
                state = "  queued %d/%d" % (where[1], where[2])
            elif steps < 2 or preview.cached_merge(self.folder, f, radius,
                                                   width=None):
                state = ""
            else:
                state = "  pending"
            # Every label in its own colour, because they are independent
            # facts and a row that can only be one colour makes them queue up.
            # A shot left out is the exception: that is about the whole row,
            # so the whole row goes dim.
            #
            # The exposures the merge spans, because two shots with the same
            # frame count can be different brackets entirely -- and on this
            # material the one that matters, the burst with its own ladder, is
            # invisible in a count. The extremes and not all five: five
            # shutter speeds are thirty characters of their own.
            #
            # `#5` is what it always was -- the frames going into the merge
            # -- and the missing ones are said in a word rather than folded
            # into that count: a bracket cut short can borrow its way back to
            # five, and `3/5` would then be a lie about both.
            out = f["include"]
            parts = [(mark, "#4a4c53" if not out
                      else self.SYNC_COLOUR.get(sync, "#8a8a90")),
                     (" %3d" % f["index"],
                      GONE if not out else MANUAL if f.get("manual")
                      else "#e8e8ea"),
                     (" %s" % f["time"][11:19], "#4a4c53" if not out else "#8a8a90"),
                     (" #%d" % steps, "#4a4c53" if not out else "#e8e8ea")]
            # In a word and not only in the glyph: a green diamond among grey
            # circles is findable once you know it is there, and invisible
            # until then. `partial` earned its word the same way.
            if sync in ("fast", "slow"):
                parts.append((" %s" % sync, "#4a4c53" if not out
                              else self.SYNC_COLOUR[sync]))
            if short:
                parts.append((" partial", "#4a4c53" if not out else GONE))
            # The radius is not marked at all. It was an asterisk, then pink,
            # and both were answering a question the row already answers: the
            # number of a shot you have corrected is pink, and the tooltip
            # says which of its settings you set. Marking the radius as well
            # said "somebody touched this" twice in one line.
            parts += [(" %s" % self.span(f), "#4a4c53" if not out else "#8a8a90"),
                      (" r%s" % (radius or "—"),
                       "#4a4c53" if not out else "#e8e8ea")]
            if state:
                parts.append((state, "#4a4c53" if not out else DETECTED))
            # A row is dense on purpose -- four facts in forty characters --
            # and every one of them is a symbol somebody has to be told about
            # once. The tooltip is where they are told: it reads the row back
            # in words, which is cheaper than a legend nobody would find.
            says = ["set %d" % f["index"], f["time"][11:19]]
            says.append("%d frames in the merge" % steps if steps > 1
                        else "developed on its own")
            if sync:
                says.append({"fast": "fired back to back",
                             "normal": "the ordinary cadence between frames",
                             "slow": "slow between frames"}[sync])
            if short:
                says.append("%d frame%s short of the brackets like it"
                            % (short, "" if short == 1 else "s"))
            if radius:
                says.append("blur radius %s, %s"
                            % (radius, self.WHOSE.get(
                                (f.get("blend_radius") or {}).get("mode", "auto"),
                                "measured")))
            if f.get("manual"):
                says.append("corrected by hand")
            if not out:
                says.append("skipped")
            rows.append(("".join(t for t, _ in parts),
                         "".join('<span style="color:%s">%s</span>'
                                 % (c, t.replace(" ", "&nbsp;"))
                                 for t, c in parts),
                         " · ".join(says)))

        self.list.blockSignals(True)
        if self.list.count() != len(rows):
            self.list.clear()
            for text, _html, _says in rows:
                item = QtWidgets.QListWidgetItem(text)
                item.setFont(QtGui.QFont("Menlo", 11))
                self.list.addItem(item)
        for i, (text, html, says) in enumerate(rows):
            item = self.list.item(i)
            if item.text() != text:
                item.setText(text)
            if item.data(QtCore.Qt.UserRole + 1) != html:
                item.setData(QtCore.Qt.UserRole + 1, html)
                item.setToolTip(_tip(says))
        here = self.rows.index(self.i) if self.i in self.rows else -1
        if self.list.currentRow() != here:
            self.list.setCurrentRow(here)
        self.list.blockSignals(False)
        self.head.setText(
            '<div style="font-size:16px"><b>%s</b></div>'
            '<div style="color:#8a8a90">%s%s</div>'
            % (os.path.basename(self.folder), plan.summary(self.doc),
               " · unsaved" if self.dirty else ""))

    # -- the selected frame -----------------------------------------------
    def select_all(self):
        """Pick every frame the list is showing."""
        if self.busy_aligning():
            return
        self.list.selectAll()
        self.note("%d sets selected" % len(self.rows))

    def chosen(self):
        """The frames a batch command acts on: the selection, or the one shown."""
        rows = {i.row() for i in self.list.selectedIndexes()}
        picked = [self.rows[r] for r in sorted(rows) if 0 <= r < len(self.rows)]
        return picked if len(picked) > 1 else [self.i]

    def _row_picked(self, row):
        """A row of the list is a frame, but not always the same number.

        And while a bracket is being lined up, clicking another one asks first:
        the offsets are chosen and not yet merged, and walking away from them
        by a stray click is the one thing this mode has to make hard.
        """
        _, _, QtWidgets = _widgets()

        if not (0 <= row < len(self.rows)):
            return
        target = self.rows[row]
        if self.aligning and target != self.i:
            box = QtWidgets.QMessageBox(self.win)
            box.setWindowTitle("hdrmerge-timelapser")
            box.setText("Leave set %d?" % self.frame["index"])
            box.setInformativeText(
                "It is being lined up. Leaving finishes that: the offsets are "
                "kept and the frame merges again in the background.")
            box.setStandardButtons(QtWidgets.QMessageBox.Cancel
                                   | QtWidgets.QMessageBox.Yes)
            box.setDefaultButton(QtWidgets.QMessageBox.Cancel)
            centre(box, self.win)
            if box.exec() != QtWidgets.QMessageBox.Yes:
                here = self.rows.index(self.i) if self.i in self.rows else -1
                self.list.blockSignals(True)
                self.list.setCurrentRow(here)
                self.list.blockSignals(False)
                return
            self.align_mode()            # ends it, and merges what you set
        self.show_frame(target)

    def show_frame(self, i):
        if not (0 <= i < len(self.doc["frames"])):
            return
        if self.aligning and i != self.i:
            # Leaving a frame you have been lining up: its merge is out of
            # date, so it goes on the queue rather than being forgotten.
            leaving = self.frame["anchor"]
            if len(plan.merged_steps(self.frame)) > 1:
                self._enqueue(leaving)
                QtCore = _widgets()[0]
                QtCore.QTimer.singleShot(0, self._pump)
        self.i = i
        self.merged = None
        self.generation += 1
        self.slots.clear()
        f = self.frame
        self.list.setCurrentRow(i)
        info = f.get("blend_radius") or {}
        radius, mode = info.get("chosen"), info.get("mode", "auto")
        # `auto` alone hides the very thing a person is deciding about, so the
        # entry says what auto worked out to as soon as that is known -- and
        # says nothing else while it is not. A folder opened at a fixed radius
        # has not been scored, and inventing a number there would be worse
        # than the bare word.
        auto = self.auto_radius(f)
        self.radius.setItemText(0, "auto (%s)" % auto if auto else "auto")
        self.radius.setCurrentIndex(
            0 if mode == "auto"
            else max(0, [str(r) for r in RADIUS_CANDIDATES].index(str(radius)) + 1))
        self._show_why()
        # The digits this bracket actually has: `⇧1–⇧9` on a three-frame
        # bracket invites six keys that do nothing.
        self._light_sheet(f)
        for what, how in (("disable / enable frame", "⇧1–⇧%d"),
                          ("select frame", "1–%d"),
                          ("show / hide frame", "⇧1–⇧%d")):
            keys = getattr(self, "keylabels", {}).get(what)
            if keys:
                self._set_key(keys, how % len(f["steps"])
                              if len(f["steps"]) > 1 else "—")
        self._read()
        # Something is always aimed, so `d` shows a comparison the first time
        # it is pressed rather than a message about how to make one.
        order = [n for n, step in enumerate(f["steps"]) if step["use"]]
        longest = self._layer_order()
        rest = [n for n in order if f["steps"][n]["frame"] != (longest or [""])[0]]
        self.aim = rest[-1] if rest else (order[0] if order else None)
        self._fill_steps(f)
        self.merge_now(delay=120)

    def _fill_steps(self, f):
        """The bracket as a strip under the picture, one card per step.

        It was a column down the right side, which put the steps of *this
        frame* and the list of *all frames* on the same side of the window and
        the picture between them. Along the bottom they read as what they are:
        the ladder this one picture is made of.
        """
        QtCore, QtGui, QtWidgets = _widgets()

        while self.steps_box.count():
            item = self.steps_box.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        palette = self._palette()
        for n, step in enumerate(f["steps"]):
            card = QtWidgets.QFrame()
            card.setObjectName("step")
            # Only while lining up: outside the mode nothing moves the
            # selected frame, so saying which one it is answers a question
            # nobody is asking. The border is always drawn, transparent when
            # it is not wanted -- painting it only on the selected card moved
            # every card two pixels each time the selection changed.
            edge = (palette.get(step["frame"]) or (232, 232, 234)
                    if self.aligning and self.aim == n else None)
            # What the card is showing: while aligning, whether the frame is
            # on the overlay; otherwise whether it is in the merge. Hiding one
            # to see the others is not the same decision as leaving it out of
            # the result, and a card that answered only the second made the
            # first invisible.
            on = step["use"] and not (self.aligning and n in self.hidden)
            card.setStyleSheet(
                "#step{background:%s;border-radius:4px;border:2px solid %s;}"
                % ("#1b2a1c" if on else "#1a1a1c",
                   "rgb(%d,%d,%d)" % edge if edge else "transparent"))
            h = QtWidgets.QHBoxLayout(card)
            h.setContentsMargins(6, 6, 8, 6)
            h.setSpacing(8)

            colour = palette.get(step["frame"])
            if colour:
                chip = QtWidgets.QLabel()
                chip.setFixedWidth(4)
                chip.setStyleSheet("background:rgb(%d,%d,%d);border-radius:2px;%s"
                                   % (colour + ("" if on
                                                else "opacity:0.35;",)))
                h.addWidget(chip)

            pic = QtWidgets.QLabel()
            pic.setFixedSize(72, 48)
            pic.setStyleSheet("background:#0f0f11;")
            pic.setCursor(QtCore.Qt.PointingHandCursor)
            pic.setToolTip(_tip("Click to put this frame in the merge or take it out"))
            pic.mousePressEvent = lambda _e, k=n: self.toggle_step(k)
            card.mousePressEvent = lambda _e, k=n: self.toggle_step(k)
            path = self.sources.get(step["frame"])
            if path:
                self._async_thumb(path, pic, 72)
            h.addWidget(pic)

            col = QtWidgets.QVBoxLayout()
            col.setSpacing(0)
            shot = read_shot(path) if path else None
            head = QtWidgets.QHBoxLayout()
            head.setSpacing(6)
            check = QtWidgets.QCheckBox()
            check.setChecked(on)
            check.setToolTip(_tip("Merge this frame, or leave it out  (%d)" % (n + 1)))
            check.clicked.connect(lambda _c, k=n: self.toggle_step(k))
            head.addWidget(check)
            from .detect import shutter
            name = self._lab(shutter(shot.exp) if shot else step["frame"],
                             "#e8e8ea" if on else "#5a5a60", 13, True)
            name.setCursor(QtCore.Qt.PointingHandCursor)
            name.setToolTip(_tip("Show this frame on its own in the big view"))
            name.mousePressEvent = lambda _e, k=n: self.show_step(k)
            head.addWidget(name)
            head.addStretch(1)
            col.addLayout(head)
            if shot:
                col.addWidget(self._lab(
                    "ISO %s · %s" % (shot.iso or "?",
                                     shot.time.strftime("%H:%M:%S")
                                     if shot.time else "—"),
                    "#8a8a90" if on else "#5a5a60", 11))
            served = step.get("serves_percent")
            col.addWidget(self._lab(
                "resolves %.3f %%" % served if served is not None else "—",
                "#8a8a90" if on else "#5a5a60", 11))
            # Always drawn, even at zero: it is what you click to aim the
            # arrows at a step, and a control that only appears once you have
            # already used the feature is a control nobody finds.
            off = step.get("offset") or [0, 0]
            aimed = self.aligning and self.aim == n and on
            sure = step.get("offset_sure", True)
            mark = self._lab(
                ("▸ " if aimed else "") + (self._offset(step) if sure
                                           else "could not be measured"),
                MANUAL if aimed else ("#5a5a60" if not (off[0] or off[1])
                                      else DETECTED if sure else GONE), 11)
            mark.setCursor(QtCore.Qt.PointingHandCursor)
            mark.setToolTip(_tip("Press a to align this bracket, and the arrows "
                            "move whichever frame is selected"))
            mark.mousePressEvent = lambda _e, k=n: self.aim_step(k)
            col.addWidget(mark)
            if step["frame"] in self._borrowed(f):
                b = f["borrow"]
                col.addWidget(self._lab("borrowed · %s s away"
                                        % b.get("gap_seconds", "?"),
                                        REF, 11))
            elif self.aligning and n in self.hidden:
                col.addWidget(self._lab("hidden while you line them up",
                                        "#7ec8ff", size=11))
            elif not step["use"]:
                col.addWidget(self._lab(*(
                    ("nothing needs it", "#ff9f6b")
                    if served is not None and served < self._floor()
                    else ("left out by hand", "#7ec8ff")), size=11))
            col.addStretch(1)
            h.addLayout(col, 1)
            self.steps_box.addWidget(card)
        self.steps_box.addStretch(0)

    def _colour(self):
        if self.cam2out is None:
            self.cam2out = preview.colour(self.folder,
                                          next(iter(self.sources.values())))
        return self.cam2out

    def _tell(self, signal, *args):
        """Emit from a worker, unless the window it belongs to is gone.

        A merge takes twenty seconds and a person can close the window in one.
        Qt then deletes the signal object under the thread still holding it,
        and the answer arrives to `RuntimeError: Signal source has been
        deleted` -- printed once per job in flight, which is a stack trace for
        having quit.
        """
        try:
            signal.emit(*args)
        except RuntimeError:
            pass

    def _async_thumb(self, path, target, width=None, crop=None, tag=""):
        """Develop a preview off the GUI thread and drop it in when it lands.

        The label is handed over as a ticket and not as an object. A preview
        takes about a second, and in that second the frame can change and the
        widgets it belonged to are deleted -- handing the worker the label meant
        the answer arrived for something that no longer existed and Qt said so,
        in C++, three times a frame. The ticket is looked up on arrival; if the
        frame moved on, the answer is simply dropped.
        """
        QtCore, _, _ = _widgets()

        ticket = self._next_ticket = getattr(self, "_next_ticket", 0) + 1
        self.slots[ticket] = (target, width, self.generation)

        class Job(QtCore.QRunnable):
            def __init__(self, editor):
                super().__init__()
                self.editor = editor

            def run(self):
                try:
                    out = preview.thumb(self.editor.folder, path,
                                        self.editor._colour(),
                                        wb=self.editor.wb, crop=crop, tag=tag)
                except Exception as e:                      # noqa: BLE001
                    return self.editor._tell(self.editor.signals.failed,
                                             str(e))
                self.editor._tell(self.editor.signals.done, out, ticket)

        QtCore.QThreadPool.globalInstance().start(Job(self))

    def _async_view(self, path):
        """Develop one source frame to linear, off the GUI thread."""
        QtCore, _, _ = _widgets()

        ticket = self._next_ticket = getattr(self, "_next_ticket", 0) + 1
        self.slots[ticket] = (self.image, None, self.generation)
        editor = self

        class Job(QtCore.QRunnable):
            def run(self):
                try:
                    out = preview.view(editor.folder, path, editor._colour(),
                                       wb=editor.wb)
                except Exception as e:                      # noqa: BLE001
                    return editor._tell(editor.signals.failed, str(e))
                editor._tell(editor.signals.done, out, ticket)

        QtCore.QThreadPool.globalInstance().start(Job())

    def place(self, png, ticket):
        """Put a finished preview where it was asked for, if that is still there."""
        _, QtGui, _ = _widgets()
        import shiboken6

        what = self.slots.pop(ticket, None)
        if what is None:
            return
        target, width, generation = what
        if generation != self.generation or not shiboken6.isValid(target):
            return                      # the frame moved on while it developed
        if target is self.image and self.merged is not None:
            return                      # a merge got there first; it wins
        if target is self.image:
            return self._load_view(png)     # the big view speaks linear
        pix = QtGui.QPixmap(png)
        if pix.isNull():
            return
        QtCore = _widgets()[0]
        if hasattr(target, "fit"):          # the big view scales it itself
            target.setPixmap(pix)
        else:
            target.setPixmap(pix.scaledToWidth(
                width, QtCore.Qt.SmoothTransformation))
            target.setText("")

    def _show_preview(self, f):
        """What to show when there is no merge to make: nothing, or one frame.

        It used to show the frame the merge is *attributed to* whatever the
        plan said, so unticking every step but one left the wrong exposure on
        screen -- the anchor, not the step still in. And it preferred a merged
        DNG from a previous `apply` run if one existed, which after any edit is
        a picture of a plan that no longer exists. Both are gone: one step
        shows that step, no steps says so.
        """
        QtCore, QtGui, _ = _widgets()

        steps = plan.merged_steps(f)
        if not steps:
            self.image.setPixmap(QtGui.QPixmap())
            self.image.setText("no frames chosen — nothing to merge")
            self.range_text = ""
            self._read()
            return
        path = self.sources.get(steps[0])
        if not path:
            self.image.setPixmap(QtGui.QPixmap())
            self.image.setText("%s is not in this folder" % steps[0])
            self.range_text = ""
            self._read("%s is not in this folder" % steps[0])
            return
        self._read()
        self.image.setText("developing…")
        self._async_view(path)

    # -- editing ----------------------------------------------------------
    def _remember(self):
        """Keep the frame as it is, so the change about to happen can be undone.

        The index rides along with it: an undo that put the old frame back at
        whatever row happened to be selected would quietly edit the wrong one.
        A new change drops the redo stack, as everywhere else.
        """
        self.undo.append((self.i, json.dumps(self.doc["frames"][self.i])))
        del self.undo[:-50]
        self.redo = []
        self.dirty = True

    def toggle_step(self, n):
        """A frame in or out -- of the merge outside the mode, of the overlay
        inside it.

        Inside the alignment mode this must not touch the merge. Hiding a
        frame there is a way of *looking*: four frames drawn over each other
        is a mess, so you take three off to see the one you are moving. It
        used to write straight to `use`, which is the merge's own field, so
        coming out of the mode you had silently changed what would be merged
        -- and the frame you had hidden to see better was gone from the
        result. What is hidden here lives in `self.hidden` and dies with the
        mode.
        """
        f = self.frame
        if not (0 <= n < len(f["steps"])) or self.nothing_to_decide():
            return
        if self.aligning:
            if not f["steps"][n]["use"]:
                # Hiding something that is not there is a key that appears to
                # work: the overlay draws what the merge would use, and this
                # one is out of the merge.
                return self.note("%s is out of the merge, so it is not on the "
                                 "overlay" % f["steps"][n]["frame"])
            if n in self.hidden:
                self.hidden.discard(n)
            else:
                self.hidden.add(n)
            # Showing or hiding a frame is also choosing it. You reach for a
            # frame's tick because that is the one you are thinking about, and
            # having to say so twice -- tick it, then aim it -- is ceremony
            # between the thought and the arrows.
            #
            # The selection stays on it even hidden: moving it elsewhere would
            # be the arrows quietly picking up a different frame, which is a
            # worse surprise than being told this one is hidden.
            self.aim = n
            self.note("%s %s" % (f["steps"][n]["frame"],
                                 "hidden" if n in self.hidden else "shown"))
            self._fill_steps(f)
            self._read()
            self._paint()
            return
        self._remember()
        f["steps"][n]["use"] = not f["steps"][n]["use"]
        self._spans.pop(f["anchor"], None)
        plan.mark(f, "steps")
        self.note("frame %s %s by hand"
                  % (f["steps"][n]["frame"],
                     "in" if f["steps"][n]["use"] else "out"))
        self._fill_steps(f)
        self.merge_now()
        self.reload_list()

    def set_radius(self):
        """Take the radius from the combo, by position and not by its words.

        Entry zero reads `auto (50)` -- it says what auto worked out, which is
        the whole point of it -- and the code compared that against the string
        "auto". So choosing auto by hand raised instead of choosing anything,
        and every path that walked the ladder by text raised with it.
        """
        if self.busy_aligning() or self.nothing_to_decide():
            return
        if self.radius.currentIndex() == 0:
            self.measure_radius(self.chosen() or [self.i])
        many = self.chosen()
        if len(many) > 1:
            return self._radius_to(many)
        f = self.frame
        self._remember()
        index = self.radius.currentIndex()
        text = "auto" if index <= 0 else str(RADIUS_CANDIDATES[index - 1])
        if text == "auto":
            f["blend_radius"]["mode"] = "auto"
            candidates = f["blend_radius"].get("candidates") or {}
            best = [r for r in RADIUS_CANDIDATES
                    if candidates.get(str(r), 100) <= 0.1]
            f["blend_radius"]["chosen"] = best[-1] if best else RADIUS_CANDIDATES[0]
            if "blend_radius" in f["manual"]:
                f["manual"].remove("blend_radius")
        else:
            f["blend_radius"]["chosen"] = int(text)
            f["blend_radius"]["mode"] = "manual"
            plan.mark(f, "blend_radius")
        self.note("blur radius %s" % self.radius.currentText())
        self._show_why()
        self.merge_now()
        self.reload_list()

    def _radius_to(self, frames):
        """Give a whole selection the radius the combo is showing."""
        QtCore, _, _ = _widgets()

        index = self.radius.currentIndex()
        for i in frames:
            f = self.doc["frames"][i]
            self.undo.append((i, json.dumps(f)))
            info = f.setdefault("blend_radius", {})
            if index <= 0:
                auto = self.auto_radius(f)
                if auto is not None:
                    info["chosen"] = auto
                info["mode"] = "auto"
                if "blend_radius" in f["manual"]:
                    f["manual"].remove("blend_radius")
            else:
                info["chosen"] = RADIUS_CANDIDATES[index - 1]
                info["mode"] = "manual"
                plan.mark(f, "blend_radius")
            if len(plan.merged_steps(f)) > 1 and not preview.cached_merge(
                    self.folder, f, info.get("chosen") or 3, width=None):
                self._enqueue(f["anchor"])
        del self.undo[:-50]
        self.redo = []
        self.dirty = True
        self.note("blur radius %s on %d sets"
                  % (self.radius.currentText(), len(frames)))
        self._show_why()
        QtCore.QTimer.singleShot(0, self._pump)
        self.reload_list()

    def cycle_radius(self, step=1):
        """The next blur radius up or down, wrapping round the ladder.

        `auto` is one of the entries and not a mode outside them, so walking
        down from 3 lands on it: the ladder reads auto, 3, 5, 10, 25, 50, 100,
        250, 500 and then back to auto.
        """
        if self.busy_aligning():
            return
        self.radius.setCurrentIndex(
            (self.radius.currentIndex() + step) % self.radius.count())
        self.set_radius()

    def toggle_include(self):
        """Drop this frame from the sequence, or put it back. Bound to DEL.

        There was a checkbox for this in the panel of merge settings, which is
        the wrong place twice over: leaving a frame out is not a property of
        how it merges, and a list of frames is a thing you delete from with the
        delete key -- which is what `eclipse-aligner` does, and these are the
        same person's tools. The list dims what is out; DEL again brings it
        back.
        """
        if self.busy_aligning():
            return
        f = self.frame
        self._remember()
        f["include"] = not f["include"]
        plan.mark(f, "include")
        self.note("set %d skipped — DEL puts it back" % f["index"]
                  if not f["include"] else "set %d back in" % f["index"])
        self.reload_list()

    def revert(self):
        """Put the frame -- or every selected one -- back as detect had it.

        It used to only clear the `manual` marks, which meant a button labelled
        `back to auto` that changed nothing you could see: the steps and the
        radius you had set stayed set, and the frame merely stopped being
        protected from the next detect. Nobody could have guessed that from the
        words, which is why they were the ones asked about.

        Now it rebuilds the frame from what detect measured and left in the
        plan: the coverage of every step, and the score of every radius.
        """
        if self.busy_aligning():
            return
        many = self.chosen()
        if len(many) > 1:
            here = self.i
            for i in many:
                self.show_frame(i)
                self._revert_one()
            self.show_frame(here)
            self.note("%d sets put back as detect measured them" % len(many))
            return self.reload_list()
        self._revert_one()

    def measure_radius(self, frames):
        """Score the radius ladder for these sets, now, and keep the numbers.

        A folder opened at a fixed radius carries no scores: measuring them is
        half a second a set and the answer was not asked for. This is where it
        is asked for -- `⌘D`, or choosing `auto` in the combo -- so it is paid
        here, once, and written into the plan so nothing pays it twice.
        """
        from .detect import choose_radius
        from .develop import read_cfa

        QtCore, _, QtWidgets = _widgets()
        todo = [i for i in frames
                if not ((self.doc["frames"][i].get("blend_radius") or {})
                        .get("candidates"))
                and self.bracketed(self.doc["frames"][i])]
        if not todo:
            return True
        self.note("measuring the radius on %d set%s…"
                  % (len(todo), "" if len(todo) == 1 else "s"), flash=False)
        QtWidgets.QApplication.processEvents()
        for i in todo:
            f = self.doc["frames"][i]
            shots = [read_shot(self.sources[s["frame"]])
                     for s in f["steps"]
                     if s["use"] and self.sources.get(s["frame"])]
            if len(shots) < 2:
                continue
            _, scores, cap = choose_radius(shots, read_cfa)
            info = f.setdefault("blend_radius", {})
            info["candidates"] = scores
            info["cap"] = cap
            self.dirty = True
        return True

    def autodetect(self):
        """Autodetect the HDR settings of these frames: the blur radius and
        which exposures are worth merging.

        The same two judgements `detect` makes with `measured`, applied later
        and to a selection -- because a folder opened with a fixed radius has
        every score in its plan already: the coverage of every exposure and
        what every candidate radius would spoil. Nothing is read again, so this
        is instant however many frames are picked.

        It leaves alone what it never decided: the alignment offsets, whether a
        frame is in the sequence, the anchor. That is the difference from
        `reset this bracketed set`, which puts everything back.
        """
        from .detect import exposure_factor, useful_steps

        if self.busy_aligning():
            return
        QtCore, _, _ = _widgets()

        many = [i for i in self.chosen()
                if self.bracketed(self.doc["frames"][i])]
        if not many:
            return self.note("a single frame: nothing to merge, so there is "
                             "nothing to measure")
        self.measure_radius(many)
        floor = self._floor() / 100.0
        changed = 0
        for i in many:
            f = self.doc["frames"][i]
            if len(f["steps"]) < 2:
                continue
            self.undo.append((i, json.dumps(f)))
            before = (tuple(plan.merged_steps(f)),
                      (f.get("blend_radius") or {}).get("chosen"))
            shots = [(read_shot(self.sources[s["frame"]]),
                      (s.get("serves_percent") or 0) / 100.0)
                     for s in f["steps"] if self.sources.get(s["frame"])]
            shots.sort(key=lambda pair: exposure_factor(pair[0]), reverse=True)
            if shots:
                keep, _ = useful_steps([sh for sh, _ in shots], shots, floor)
                kept = {sh.name for sh, _ in keep}
                for step in f["steps"]:
                    step["use"] = step["frame"] in kept
            info = f.setdefault("blend_radius", {})
            auto = self.auto_radius(f)
            if auto is not None:
                info["chosen"] = auto
            info["mode"] = "auto"
            if "blend_radius" in f["manual"]:
                f["manual"].remove("blend_radius")
            if before != (tuple(plan.merged_steps(f)), info.get("chosen")):
                changed += 1
            self._spans.pop(f["anchor"], None)
            if len(plan.merged_steps(f)) > 1 and not preview.cached_merge(
                    self.folder, f, info.get("chosen") or 3, width=None):
                self._enqueue(f["anchor"])
        del self.undo[:-50]
        self.redo = []
        self.dirty = True
        self.note("measured %d set%s, %d changed"
                  % (len(many), "" if len(many) == 1 else "s", changed))
        self.show_frame(self.i)
        QtCore.QTimer.singleShot(0, self._pump)
        self.reload_list()

    def _revert_one(self):
        """The reset itself, on the frame being shown."""
        from .detect import useful_steps

        f = self.frame
        self._remember()
        # Back to how the folder was opened, which is not the same as back to
        # what the measurement would say. Open at a fixed radius and nothing
        # is pruned and every bracket gets that radius; a reset that handed
        # back auto's answer was undoing a decision nobody had made here.
        opened_at = (self.doc.get("settings") or {}).get("blend_radius")
        opened_at = None if opened_at in (None, "auto") else int(opened_at)
        floor = self._floor() / 100.0
        # Longest exposure first, which is the order `useful_steps` reads: it
        # keeps the first entry unconditionally and drops from the short end.
        # Handed the steps in capture order it kept the 1/8000 as the one that
        # must survive and pruned nothing -- a reset that reset upwards.
        from .detect import exposure_factor

        shots = [(read_shot(self.sources[s["frame"]]),
                  (s.get("serves_percent") or 0) / 100.0)
                 for s in f["steps"] if self.sources.get(s["frame"])]
        shots.sort(key=lambda pair: exposure_factor(pair[0]), reverse=True)
        if opened_at:
            for step in f["steps"]:
                step["use"] = True
        elif shots:
            keep, _ = useful_steps([sh for sh, _ in shots], shots, floor)
            kept = {sh.name for sh, _ in keep}
            for step in f["steps"]:
                step["use"] = step["frame"] in kept
        info = f.get("blend_radius") or {}
        if opened_at:
            info["chosen"], info["mode"] = opened_at, "fixed"
        else:
            auto = self.auto_radius(f)
            if auto is not None:
                info["chosen"] = auto
            info["mode"] = "auto"
        f.pop("offset", None)
        for step in f["steps"]:
            for gone in ("offset", "offset_peak", "offset_sure"):
                step.pop(gone, None)
        f.pop("align", None)
        f["include"] = True
        f["manual"] = []
        self._spans.pop(f["anchor"], None)
        self.aligning = False
        self.was = None
        self.hidden = set()
        self.sheets.setCurrentIndex(0)
        self.set_view(0)
        self.note("set %d put back as detect measured it: frames, radius, "
                  "offsets and all" % f["index"])
        self.show_frame(self.i)
        self.reload_list()

    def align_steps(self):
        """Measure where each step of this bracket sits, and move it there.

        Phase correlation against the longest exposure, which is the one with
        the most to correlate. A step the measurement cannot trust is left
        where it is and says so on its card -- on a totality bracket the
        1/8000 is black, and a confident-looking answer from it would move the
        one frame that carries the limb.
        """
        from . import align

        f = self.frame
        steps = plan.merged_steps(f)
        if len(steps) < 2:
            return self.note("one frame only: nothing to align")
        self._remember()
        order = self._layer_order()
        reference = self.sources[order[0]]
        self.note("measuring the offsets…", flash=False)
        _, _, QtWidgets = _widgets()
        QtWidgets.QApplication.processEvents()
        found = align.measure([self.sources[n] for n in steps], reference)
        moved = unsure = 0
        for step in f["steps"]:
            got = found.get(step["frame"])
            if not got:
                continue
            step["offset"] = [got["dx"], got["dy"]]
            step["offset_peak"] = got["peak"]
            step["offset_sure"] = got["sure"]
            moved += bool(got["dx"] or got["dy"])
            unsure += not got["sure"]
        plan.mark(f, "steps")
        self.note("%d frame%s moved%s — press a to see them against each other"
                  % (moved, "" if moved == 1 else "s",
                     ", %d could not be measured" % unsure if unsure else ""))
        self._fill_steps(f)
        self.merge_now(delay=0)
        self.reload_list()

    def align_mode(self):
        """Go in and out of lining the bracket up. `a` goes in, ↩ comes out.

        A mode and not a held key, because this is the one job here that takes
        both hands and a minute: you aim a step, walk it two pixels at a time,
        and look. Held, every glance at the merge cost a ten-second re-merge of
        offsets you had not finished choosing. So nothing merges while this is
        on, and leaving it merges once, with what you settled on.
        """
        if self.aligning:
            return self._leave_align("alignment applied")
        if len(plan.merged_steps(self.frame)) < 2:
            return self.note("one frame only: nothing to align")
        self.aligning = True
        self.hidden = set()
        self.was = (self.i, json.dumps(self.doc["frames"][self.i]),
                    len(self.undo), self.dirty)
        self.sheets.setCurrentIndex(1)
        self.set_view(4)
        self._fill_steps(self.frame)
        self.note("aligning")

    def _leave_align(self, why):
        """Out of the mode and back to the merge, whichever way you left."""
        self.aligning = False
        self.was = None
        self.hidden = set()
        self.sheets.setCurrentIndex(0)
        self.set_view(0)
        self._fill_steps(self.frame)
        self.note(why)
        self.merge_now(delay=0)

    def enter_align(self):
        """`a` goes in. Getting out is return or escape, and nothing else."""
        if not self.aligning:
            self.align_mode()

    def apply_align(self):
        """Return: keep what was moved and leave the mode.

        Only inside the mode. Return outside it would be a key that starts a
        mode without being asked, which is the sort of thing a person presses
        while reading a list.
        """
        if self.aligning:
            self.align_mode()

    def cancel_align(self):
        """Esc: leave the mode with every offset as it was on the way in.

        Not the same as `⇧K`, which clears the bracket outright: a frame
        aligned last week keeps what it had, and only this visit is undone.
        The moves made here come off the undo stack with it -- an undo that
        walked back into a session you cancelled would be putting back
        something you just said you did not want.
        """
        if not self.aligning:
            return
        if not self.was:
            return self._leave_align("back to the merge")
        i, was, depth, dirty = self.was
        self.doc["frames"][i] = json.loads(was)
        del self.undo[depth:]
        self.redo = []
        self.dirty = dirty
        self.aim = None
        self._leave_align("alignment cancelled")
        self.show_frame(i)
        self.reload_list()

    def aim_step(self, n):
        """Choose which step the nudging keys move."""
        if not (0 <= n < len(self.frame["steps"])):
            return
        self.aim = n
        self._fill_steps(self.frame)
        shot = self.sources.get(self.frame["steps"][n]["frame"])
        from .detect import shutter
        self.note("the arrows move %s"
                  % (shutter(read_shot(shot).exp) if shot else "this frame"))
        self._read()
        # And redraw: the aimed exposure is the one painted on top, so
        # choosing another one changes the picture. It did not, because
        # nothing here asked for it -- the order was right and invisible.
        self._paint()

    def _drag_step(self, dx, dy, phase):
        """Shift and drag on the picture: what is being aligned follows it.

        Only inside the mode, and only the aimed step: dragging the finished
        frame around moved it against its neighbours, which is registration,
        and registration is `eclipse-aligner`'s job.

        The preview is a quarter of the raw, so a pixel dragged here is four
        there; the movement is rounded to a whole Bayer cell, the same step
        the arrows take, because that is the shift that costs no sharpness.

        Repainting on every mouse move would mean recomputing every step's
        edges at pointer speed, which is seconds of work for a picture nobody
        would see. The offset moves immediately -- the read line follows -- and
        the overlay redraws on a short timer, so a whole drag costs a handful
        of frames.
        """
        QtCore, _, _ = _widgets()

        if not self.aligning or self.aim is None:
            return
        if phase == "start":
            self._remember()
            return
        if phase == "end":
            return self._draw_soon(0)
        k = preview.LINEAR_STEP
        what = self.frame["steps"][self.aim]
        was = what.get("offset") or [0, 0]
        what["offset"] = [round((was[0] + dx * k) / 2.0) * 2,
                          round((was[1] + dy * k) / 2.0) * 2]
        plan.mark(self.frame, "steps")
        self.note("%s at %s" % (what["frame"], self._offset(what)), flash=False)
        self._draw_soon(120)

    def _draw_soon(self, delay):
        """Redraw the overlay once, however many moves arrive meanwhile."""
        QtCore, _, _ = _widgets()

        if getattr(self, "_drawing", False):
            return
        self._drawing = True

        def draw():
            self._drawing = False
            self._light_sheet(self.frame)
            self._fill_steps(self.frame)
            self._read()
            self._paint()

        QtCore.QTimer.singleShot(delay, draw)

    def nudge(self, dx, dy):
        """Move the selected frame, a whole Bayer cell at a time.

        The offset is applied sub-plane by sub-plane, so an even shift is
        exact: the same photosites, somewhere else. Everything that moves a
        frame here is even -- the arrows, the mouse, and what the measurement
        returns -- because a fractional shift has to interpolate inside one
        colour of the mosaic, and paying sharpness for a fraction of a pixel
        is a bad trade on a limb this sharp. A plan written before that was
        settled may still carry one; it is read and applied as it stands.
        """
        f = self.frame
        if self.aim is None or self.aim >= len(f["steps"]):
            return self.note("click a frame's offset first")
        step = f["steps"][self.aim]
        if self.aim in self.hidden:
            return self.note("frame %d is hidden — ⇧%d shows it again, or "
                             "pick another one to move"
                             % (self.aim + 1, self.aim + 1))
        if not step["use"]:
            # Moving something nobody can see is moving it blind: the picture
            # would not change and the offset would, which is the one way this
            # mode can lie. So it says which two keys get out of it -- a
            # refusal that does not say what to do instead is a dead end.
            return self.note("frame %d is hidden — ⇧%d shows it again, or "
                             "pick another one to move"
                             % (self.aim + 1, self.aim + 1))
        self._remember()
        was = step.get("offset") or [0, 0]
        step["offset"] = [round(was[0] + dx, 2), round(was[1] + dy, 2)]
        step["offset_sure"] = True
        plan.mark(f, "steps")
        self.note("%s at %s" % (step["frame"], self._offset(step)))
        self._fill_steps(f)
        if self.aligning:
            self._read()
            return self._paint()      # the overlay is the point; merge later
        self.merge_now(delay=1500)

    def undo_last(self):
        if not self.undo:
            return self.note("nothing to undo")
        i, was = self.undo.pop()
        self.redo.append((i, json.dumps(self.doc["frames"][i])))
        self.doc["frames"][i] = json.loads(was)
        self.dirty = True
        self.note("undone")
        self.show_frame(i)
        self.reload_list()

    def redo_last(self):
        if not self.redo:
            return self.note("nothing to redo")
        i, was = self.redo.pop()
        self.undo.append((i, json.dumps(self.doc["frames"][i])))
        self.doc["frames"][i] = json.loads(was)
        self.dirty = True
        self.note("redone")
        self.show_frame(i)
        self.reload_list()

    # -- the picture, which follows the plan without being asked -----------
    #
    # Every control that changes what HDRMerge would be given puts the frame on
    # a queue; a couple of workers take from it in order. Nothing is dropped and
    # nothing is conflated: a frame you edited and walked away from still gets
    # merged, and is waiting for you when you come back. What the queue costs in
    # wasted work it gives back in a window that never lies about what it has --
    # every frame carries its state, `queued 3/7`, `merging`, or nothing at all
    # because its picture is made.

    def _state(self, f=None):
        """What the picture of a frame should be, as a comparable key."""
        f = self.frame if f is None else f
        return (f["anchor"], tuple(plan.merged_steps(f)),
                (f.get("blend_radius") or {}).get("chosen"))

    def _by_anchor(self, anchor):
        return next((f for f in self.doc["frames"] if f["anchor"] == anchor), None)

    def queue_place(self, f):
        """Where this frame stands: None, ("merging",) or ("queued", n, total)."""
        a = f["anchor"]
        if a in self.running:
            return ("merging",)
        if a in self.queue:
            return ("queued", self.queue.index(a) + 1, len(self.queue))
        return None

    def merge_now(self, delay=350):
        """Ask for this frame's picture, queueing the merge if one is needed.

        Called by everything that changes the merge -- a step, the radius, the
        frame you are looking at. A picture that already exists goes up at once;
        anything else joins the queue, and the frame says so until it does not
        have to.
        """
        QtCore, _, _ = _widgets()

        f = self.frame
        steps = plan.merged_steps(f)
        if len(steps) < 2:
            self._drop(f["anchor"])
            self.merged = None
            self._show_preview(f)            # one exposure: just develop it
            return self.reload_list()
        radius = (f.get("blend_radius") or {}).get("chosen") or 3
        hit = preview.cached_merge(self.folder, f, radius, width=None)
        if hit:
            self._drop(f["anchor"])
            self._place_merge(hit, self._state(f), -1.0)
            return self.reload_list()
        # Nothing goes in the view but the state. It used to put the anchor
        # frame up while the merge ran -- a different picture, unlabelled, in
        # the place where the answer appears -- so the wait looked like a
        # result. What is true right now is that there is no picture yet, and
        # where in the queue it is.
        self.merged = None
        self._enqueue(f["anchor"])
        self._say_waiting(f)
        QtCore.QTimer.singleShot(delay, self._pump)
        self.reload_list()

    def _enqueue(self, anchor):
        if anchor not in self.queue and anchor not in self.running:
            self.queue.append(anchor)

    def _drop(self, anchor):
        if anchor in self.queue:
            self.queue.remove(anchor)

    def _say_waiting(self, f=None):
        """Put the state of this frame's merge where its picture will go."""
        _, QtGui, _ = _widgets()
        import time

        f = self.frame if f is None else f
        where = self.queue_place(f)
        if not where or self.view == 4:
            # While two steps are laid on top of each other, that is what the
            # view is for: a merge running in the background must not paint
            # over the thing being aligned.
            return
        steps, radius = plan.merged_steps(f), self._state(f)[2] or 3
        # Clearing comes first: a null pixmap set *after* the text wipes the
        # text with it, which left the state written into an empty label.
        self.image.setPixmap(QtGui.QPixmap())
        if where[0] == "merging":
            waited = int(time.time() - self.started.get(f["anchor"], time.time()))
            self._read("merging %d frames at r%s… %d s"
                       % (len(steps), radius, waited))
            self.image.setText("merging %d frames at r%s\n\n%d s so far"
                               % (len(steps), radius, waited))
        else:
            self._read("queued %d of %d · %d frames at r%s"
                       % (where[1], where[2], len(steps), radius))
            self.image.setText("waiting to merge — %d of %d in the queue\n\n"
                               "%d frames at r%s"
                               % (where[1], where[2], len(steps), radius))

    def _pump(self):
        """Start as many queued merges as there are free workers."""
        QtCore, _, _ = _widgets()
        from .apply import resolve_hdrmerge

        editor = self
        while self.queue and len(self.running) < MERGE_JOBS:
            anchor = self.queue.pop(0)
            live = self._by_anchor(anchor)
            if live is None or len(plan.merged_steps(live)) < 2:
                continue
            import time

            want = self._state(live)
            self.started[anchor] = time.time()
            # A frozen copy: the job reads it from another thread while the
            # window keeps editing the live one.
            frame = json.loads(json.dumps(live))
            radius = want[2] or 3
            self.running[anchor] = want

            class Job(QtCore.QRunnable):
                def __init__(self, frame, radius, anchor, want):
                    super().__init__()
                    self.frame, self.radius = frame, radius
                    self.anchor, self.want = anchor, want

                def run(self):
                    import time
                    started = time.time()
                    try:
                        png = preview.merge_preview(
                            editor.folder, self.frame, editor.sources,
                            self.radius, resolve_hdrmerge(None),
                            editor._colour(), wb=editor.wb)
                    except Exception as e:                  # noqa: BLE001
                        return editor._tell(editor.signals.merge_failed,
                                            self.anchor,
                                            "could not merge: %s" % e)
                    editor._tell(editor.signals.merged, self.anchor, png,
                                 time.time() - started)

            self.merge_pool.start(Job(frame, radius, anchor, want))
        self.reload_list()
        where = self.queue_place(self.frame)
        if where:
            self._say_waiting(self.frame)

    def _merge_failed(self, anchor, message):
        """A merge that did not happen still frees its worker."""
        self.running.pop(anchor, None)
        self.note(message)
        self._pump()

    def _merge_arrived(self, anchor, png, seconds):
        """A merge finished. Show it if it is still the answer to the question."""
        want = self.running.pop(anchor, None)
        live = self._by_anchor(anchor)
        if live is not None and want is not None and self._state(live) != want:
            self._enqueue(anchor)       # it was edited mid-flight: do it again
        elif live is not None and live is self.frame:
            self._place_merge(png, want, seconds)
        self._pump()

    def _sync(self, bar, value):
        """Move a slider without it calling us straight back."""
        if bar.value() != value:
            bar.blockSignals(True)
            bar.setValue(value)
            bar.blockSignals(False)

    def set_exposure(self, tenths):
        """The exposure slider: re-tonemap what is already in memory."""
        tenths = int(tenths)
        self._sync(self.slider, tenths)
        self.ev = tenths / 10.0
        self.evlab.setText("%+.1f EV" % self.ev if self.ev else "0.0 EV")
        self._paint()

    def set_detail(self, percent):
        """One knob for both halves of the same gesture, because half of it is
        useless on its own.

        Measured on a totality merge, in display levels out of 255, over the
        outer corona (2.2 to 3.2 lunar radii): flattening the base alone lifts
        it from 26 to 40 and adds no structure at all -- a grey picture, which
        is what made it feel like it did nothing. Amplifying the detail alone
        takes its local contrast from 0.71 to 2.23 and leaves the whole region
        sitting at 26, so the structure is there and too dark to read.
        Together: 40 and 2.02.

        So they move together. The base is flattened a little more than the
        detail is raised, which is what keeps the limb from going flat while
        the corona comes up.
        """
        percent = int(percent)
        self._sync(self.detail_slider, percent)
        self.detail = percent / 100.0
        self.compress = 0.85 * self.detail
        self.detaillab.setText("%d %%" % percent)
        self._paint()

    def reset_tone(self):
        """Back to the plain view: no gain, no compression, no lifted detail."""
        self.slider.setValue(0)
        self.detail_slider.setValue(0)
        self.note("showing it plain")

    def set_view(self, which=None):
        """Choose what the big view draws: the picture, or a fact about it."""
        self.view = int(which) if which is not None else self.view
        if self.view:
            self.note("showing %s — let go for the picture" % VIEWS[self.view],
                      flash=False)
        self._read()
        self._paint()

    # `l` gives the map that decides the merge -- the one the radius leaves --
    # and shift the raw one underneath it. The blurred map is what a person is
    # actually judging when they change the radius, so it is the one without a
    # modifier.
    HELD = {"c": 1, "l": 3, "L": 2}

    # Shift and a digit, which does not arrive as a digit: hold shift and the
    # keyboard sends the symbol printed above the number, so `Shift+1` never
    # matches. The symbols are read instead. Where two layouts disagree --
    # `&` is 6 on a Spanish keyboard and 7 on an American one, `(` is 8 and 9
    # -- the Spanish reading wins, because that is the keyboard this is used on.
    SHIFTED = {}

    def _shifted_digit(self, event):
        """Shift with a number key: that step in or out of the merge."""
        QtCore, _, _ = _widgets()

        if not self.SHIFTED:
            k = QtCore.Qt
            self.SHIFTED.update({
                k.Key_Exclam: 0, k.Key_QuoteDbl: 1, k.Key_At: 1,
                k.Key_periodcentered: 2, k.Key_NumberSign: 2, k.Key_sterling: 2,
                k.Key_Dollar: 3, k.Key_Percent: 4,
                k.Key_Ampersand: 5, k.Key_AsciiCircum: 5,
                k.Key_Slash: 6, k.Key_ParenLeft: 7, k.Key_Asterisk: 7,
                k.Key_ParenRight: 8,
            })
        if not event.modifiers() & QtCore.Qt.ShiftModifier:
            return False
        key = event.key()
        n = (key - QtCore.Qt.Key_1 if QtCore.Qt.Key_1 <= key <= QtCore.Qt.Key_9
             else self.SHIFTED.get(key))
        if n is None:
            return False
        self.toggle_step(n)
        return True

    def _hold(self, key, mods):
        """Show a mask for as long as the key is down."""
        QtCore, _, _ = _widgets()

        name = {QtCore.Qt.Key_C: "c", QtCore.Qt.Key_L: "l"}.get(key)
        if not name:
            return
        if name == "l" and mods & QtCore.Qt.ShiftModifier:
            name = "L"
        want = self.HELD[name]
        if self.view != want:
            self.set_view(want)

    def _let_go(self, key):
        """And back to the picture the moment it comes up."""
        QtCore, _, _ = _widgets()

        # Only the held views come back on their own, and they come back to
        # whatever was underneath: peeking at the layer map in the middle of
        # lining a bracket up must not end the lining up.
        if key in (QtCore.Qt.Key_C, QtCore.Qt.Key_L) and 0 < self.view < 4:
            self.set_view(4 if self.aligning else 0)

    def toggle_marks(self):
        self.set_view(1 if self.view != 1 else 0)

    def _by_exposure(self, names):
        """Those steps, most exposed first -- HDRMerge's own layer order."""
        from .detect import exposure_factor

        shots = [(n, read_shot(self.sources[n])) for n in names
                 if self.sources.get(n)]
        shots.sort(key=lambda pair: exposure_factor(pair[1]), reverse=True)
        return [n for n, _ in shots]

    def _layer_order(self):
        """The steps in the merge, in the order HDRMerge stacks them."""
        return self._by_exposure(plan.merged_steps(self.frame))

    def _palette(self):
        """A colour per step of this bracket, keyed to the step, not the merge.

        Taking the colour from the layer's position in the merge meant every
        colour shifted the moment you unticked a step: the 1/15 turned from
        green to blue because it had been promoted to layer 0, and the picture
        you were comparing against changed its legend under you. The rank is
        taken over **every** step of the frame, used or not, so a given
        exposure keeps its colour for as long as the bracket does.
        """
        ladder = self._by_exposure([s["frame"] for s in self.frame["steps"]])
        return {name: LAYER_COLOURS[i % len(LAYER_COLOURS)]
                for i, name in enumerate(ladder)}

    def _against_longest(self):
        """The aimed step in red over the other steps in green, offsets applied.

        Every ticked step is drawn **in its own colour** -- the same one on its
        card and in the layer views -- so the strip is the legend and there is
        nothing to remember.

        They are stacked rather than added, and the order is the point: the
        step you are moving is always on top, and the rest go behind it from
        the shortest exposure down, leaving the longest at the very back. Added
        together, five layers of edges made a pale sum in which nothing could
        be told from anything; stacked, the line you are moving is the one you
        see, and the others show through wherever it is not.

        What is compared is edges, not brightness: these are exposures three
        stops apart, and laid on top of each other by brightness the darker one
        simply is not there.
        """
        import numpy as np

        f = self.frame
        if self.aim is None or self.aim >= len(f["steps"]):
            return None
        aimed = f["steps"][self.aim]
        mine_name = aimed["frame"]
        # What is drawn is what the merge would use, minus whatever you have
        # hidden to see better. Hiding is about this overlay and lasts as long
        # as the mode; the tick that decides the merge is not touched here.
        drawn = [step for n, step in enumerate(f["steps"])
                 if step["use"] and n not in self.hidden]
        if not drawn:
            # Everything hidden is a legitimate thing to ask for -- it is how
            # you check that what you are looking at is the frame you think.
            # It has to be black, not a fallback to the merged picture, which
            # would answer a question nobody asked.
            return (np.zeros(self.lin.shape[:2] + (3,), "uint8")
                    if self.lin is not None else None)
        if not self.sources.get(mine_name):
            return None

        def layer(path, offset=(0, 0)):
            """One frame as an intensity, 0 to 255, ready to be tinted.

            Two ways of looking, on `x`. **Edges** is where the picture
            changes, which is all alignment is about, and it is the one you
            work in: five thin lines can be told apart, five pictures cannot.
            **Layers** is the frame itself, and it is worth a look because
            edges throw away everything but 4 % of the picture -- so the eye
            has no way to tell a limb that is genuinely doubled from a limb
            whose second line is grain that survived the threshold.

            Both go through the same tone curve first, because the ladder is
            three stops a step: by brightness alone the short exposure is not
            there at all.
            """
            lin, _ = preview.load_linear(
                preview.view(self.folder, path, self._colour(), wb=self.wb))
            g = lin.mean(2)
            g = np.log1p(g / (float(np.percentile(g, 99.7)) or 1.0) * 200.0)
            from scipy.ndimage import shift as ndshift, uniform_filter

            if self.overlay == "layers":
                # Not the log curve the other view uses: four layers lifted
                # that far and added come to white from corner to corner --
                # measured, 99.9 % of the picture with ink on it. The floor is
                # the frame's own median, so the sky of every exposure lands
                # on black and only what is brighter than its own background
                # is drawn.
                lo, hi = (float(x) for x in np.percentile(g, (50.0, 99.7)))
                m = np.clip((g - lo) / max(hi - lo, 1e-6), 0, 1) ** 0.5 * 255
                k = preview.LINEAR_STEP
                return ndshift(m, (offset[1] / k, offset[0] / k), order=1,
                               mode="nearest")
            g = uniform_filter(g, 3, mode="nearest")   # the noise is not an edge
            gy, gx = np.gradient(g)
            m = np.hypot(gx, gy)
            # Only the strong edges: a short exposure of a corona is grain from
            # corner to corner, and its gradient is grain too -- drawn whole it
            # filled the overlay with speckle and hid the one line that
            # matters. The floor is the 96th percentile, so what is left is the
            # brightest 4 % of the edges: the limb, and nothing else.
            floor, top = (float(x) for x in np.percentile(m, (96.0, 99.8)))
            m = np.clip((m - floor) / max(top - floor, 1e-6), 0, 1) ** 0.8 * 255
            k = preview.LINEAR_STEP
            return ndshift(m, (offset[1] / k, offset[0] / k), order=1,
                           mode="nearest")

        # Back to front: the longest exposure first, then up the ladder, and
        # the aimed step last of all so nothing can cover it.
        by_exposure = {name: k for k, name in enumerate(self._layer_order())}
        back_to_front = sorted(
            (step for step in drawn if step["frame"] != mine_name),
            key=lambda step: by_exposure.get(step["frame"], 99))
        # The selected frame goes last so nothing can cover it -- unless it is
        # hidden, and then it goes nowhere. Selected and hidden is a state you
        # reach in one keystroke, because ⇧n does both, and it used to draw
        # the one frame you had just taken off the overlay.
        if aimed["use"] and self.aim not in self.hidden:
            back_to_front.append(aimed)

        palette, out = self._palette(), None
        try:
            for step in back_to_front:
                path = self.sources.get(step["frame"])
                if not path:
                    continue
                alpha = layer(path, step.get("offset") or (0, 0)) / 255.0
                colour = np.array(palette.get(step["frame"], (255, 255, 255)),
                                  dtype="float32")
                if out is None:
                    out = alpha[..., None] * colour
                    continue
                cut = (min(out.shape[0], alpha.shape[0]),
                       min(out.shape[1], alpha.shape[1]))
                out, a = out[:cut[0], :cut[1]], alpha[:cut[0], :cut[1], None]
                # Edges stack, pictures add. A line can be covered by the line
                # in front of it and still be read from what shows either
                # side; a whole picture in front simply replaces the one
                # behind, and the overlay would be the top frame and nothing
                # else. Added, agreement goes white and every place the frames
                # do not agree keeps the colour of whichever is alone there,
                # which is the misalignment, drawn.
                out = (out + a * colour if self.overlay == "layers"
                       else out * (1.0 - a) + colour * a)
        except Exception as e:                              # noqa: BLE001
            self.note("could not read a frame: %s" % e)
            return None
        if out is None:
            return None
        return np.clip(out, 0, 255).astype("uint8")

    def _blurred_mask(self):
        """The layer map as the blur radius leaves it, not as it starts.

        What `-m` writes is the hard mask, before HDRMerge dilates it over a
        disc and runs three box passes -- so at r250 it shows a boundary that
        the merge itself smears over hundreds of pixels. This reproduces those
        two steps on the decimated mask (the same arithmetic `choose_radius`
        scores radii with), which is what actually decides where a noisy layer
        ends up.
        """
        import numpy as np
        from scipy.ndimage import distance_transform_edt, uniform_filter

        if self.mask is None:
            return None
        if self.blurred is not None:
            return self.blurred
        radius = (self.frame.get("blend_radius") or {}).get("chosen") or 3
        r = max(1, int(round(radius / preview.LINEAR_STEP)))
        idx = self.mask.astype(np.int32)
        fat = np.zeros(idx.shape, np.float32)
        for level in range(1, int(idx.max()) + 1):
            far = distance_transform_edt(~(idx >= level))
            fat = np.maximum(fat, np.where(far <= r, float(level), 0.0))
        hr = int(round(r * 0.39))                       # BoxBlur::blur
        for _ in range(3 if hr >= 1 else 0):
            fat = uniform_filter(fat, size=2 * hr + 1, mode="nearest")
        self.blurred = np.clip(np.rint(fat), 0, idx.max()).astype("uint8")
        return self.blurred

    def _load_view(self, path, is_merge=False):
        """Read a linear preview and put it on screen at the current exposure.

        Whether this is a merge is told, not inferred. It used to be read off
        `self.merged`, which `_place_merge` only sets *after* calling this --
        so the first paint of every merge treated it as a single frame and
        washed most of it blue with a noise floor that means nothing there.
        Clicking the option twice repainted it, by then correctly, which is a
        fine description of a bug and no way to run a window.
        """
        import numpy as np

        try:
            self.lin, self.levels = preview.load_linear(path)
        except Exception as e:                              # noqa: BLE001
            self.lin = None
            return self.note("could not read the preview: %s" % e)
        self.is_merge = is_merge
        self.mask = preview.load_mask(path)
        self.blurred = None
        # The reference the tone curve is normalised on, measured once per
        # picture: the slider then moves against it in stops, so 0.0 EV is
        # always "as the scene was" and the numbers on the label mean something.
        self.base = float(np.percentile(self.lin.mean(2), 99.7)) or 1.0
        self._say_range()
        self._paint()

    def _say_range(self):
        """The stops on screen, and how much of the frame the camera lost.

        The read-noise floor is only meaningful for a **single frame**. A merge
        is anchored on its shortest layer, so the whole sky sits at a few code
        values of that layer's full well even where the long exposure measured
        it perfectly -- reported against read noise, an ordinary totality merge
        came out as "97 %% in the noise", which is a fact about the file's scale
        and not about the picture. So the floor is quoted where it means
        something and the span is measured from the data either way.

        Clipping survives both cases: it is scale-free -- the value the white
        level became is scaled by exactly what scaled the pixels -- so red is
        red whether you are looking at one exposure or five.
        """
        import numpy as np

        if self.lin is None or not self.levels:
            self.range_text = ""
            return self._read()
        clip = self.levels.get("clip")
        top = self.lin.max(2)
        blown = float((top >= clip * 0.995).mean()) if clip else 0.0
        lit = top[top > 0]
        parts = []
        if lit.size:
            lo, hi = np.percentile(lit, (0.5, 99.9))
            if lo > 0:
                parts.append("%.1f EV on screen (p0.5→p99.9)"
                             % np.log2(float(hi) / float(lo)))
        if clip:
            parts.append("%.2f %% clipped" % (100 * blown))
        if not self.is_merge and self.levels.get("noise"):
            floor = self.levels["noise"] * 10.0        # SNR 10, as detect counts
            parts.append("%.0f %% under the noise floor"
                         % (100 * float((top <= floor).mean())))
        self.range_text = "  ·  ".join(parts)
        self._read()

    def _paint(self):
        """Tone-map the linear preview at the current exposure and show it."""
        import numpy as np
        QtCore, QtGui, _ = _widgets()

        if self.lin is None:
            return
        rgb = preview.tonemap(self.lin, self.base / (2.0 ** self.ev),
                              compress=self.compress, detail=self.detail)
        slide = self.frame.get("offset") or (0, 0)
        if any(slide) and self.view != 4:
            # Where the whole frame has been moved to, shown by moving the
            # picture: no merge, no develop, one array slid across.
            from scipy.ndimage import shift as ndshift

            k = preview.LINEAR_STEP
            rgb = ndshift(rgb, (slide[1] / k, slide[0] / k, 0), order=1,
                          mode="constant", cval=0)
        if self.view == 4:
            pair = self._against_longest()
            if pair is None:
                self.note("click a frame's offset first")
            else:
                rgb = pair
        elif self.view >= 2:
            layers = (self.mask if self.view == 2 else self._blurred_mask())
            if layers is None:
                self.note("no layer map here: a set of one frame has only itself"
                          if not self.is_merge else
                          "merged before layer maps were kept — touch a frame "
                          "and put it back to make one")
            elif layers.shape[:2] == rgb.shape[:2]:
                # Tinted rather than flat: which layer a pixel came from is
                # only interesting *over* what is there, so the picture stays
                # legible underneath and the colour says where it came from.
                import numpy as np

                palette, order = self._palette(), self._layer_order()
                tint = np.zeros(rgb.shape, np.float32)
                for i in range(int(layers.max()) + 1):
                    name = order[i] if i < len(order) else None
                    tint[layers == i] = palette.get(
                        name, LAYER_COLOURS[i % len(LAYER_COLOURS)])
                rgb = (0.45 * rgb.astype(np.float32)
                       + 0.55 * tint).clip(0, 255).astype("uint8")
        elif self.view == 1 and self.levels:
            # Not a filter over the picture but a statement about the data: red
            # where the sensor was at its white level and nothing there was
            # measured, blue where the signal never rose ten times over the read
            # noise. Between them is the range the merge actually resolved.
            top = self.lin.max(2)
            rgb = rgb.copy()
            if self.levels.get("clip"):
                rgb[top >= self.levels["clip"] * 0.995] = (255, 95, 86)     # GONE
            if not self.is_merge and self.levels.get("noise"):
                # Only on a single frame: on a merge this floor is the shortest
                # layer's, and it would paint the whole sky blue.
                rgb[top <= self.levels["noise"] * 10.0] = (90, 200, 250)    # DETECTED
        rgb = np.ascontiguousarray(rgb)
        h, w, _ = rgb.shape
        image = QtGui.QImage(rgb.data, w, h, 3 * w, QtGui.QImage.Format_RGB888)
        self.image.setPixmap(QtGui.QPixmap.fromImage(image.copy()))

    def _place_merge(self, path, want, seconds):
        """Put a merge in the big view and say what it is."""
        QtCore, QtGui, _ = _widgets()

        f = self.frame
        self._load_view(path, is_merge=True)
        if self.lin is None:
            return
        self.merged = want
        self._say_range()
        steps, radius = want[1], want[2] or 3
        self._read()
        self.note("merged %d frames at r%s %s"
                  % (len(steps), radius,
                     "· from the cache" if seconds < 0
                     else "in %.1f s" % seconds))

    def zoom_in(self):
        self.image.wheel(1)

    def zoom_out(self):
        self.image.wheel(-1)

    def zoom_fit(self):
        self.image.fit()

    def _arrow(self, dx, dy):
        """Lining a bracket up they move the selected frame; outside, the list.

        Two pixels a press and nothing finer. A quarter-pixel nudge was on
        shift and it is gone: an even shift moves whole photosites of every
        sub-plane and is exact, while anything else interpolates inside one
        colour of the mosaic and softens the frame -- a real cost, for a
        correction measured in whole pixels.
        """
        if not self.aligning:
            if dy:
                self.step_row(dy)
            return
        self.nudge(dx * 2, dy * 2)

    def _number(self, n):
        """A digit: which step the arrows move, and only inside the mode.

        Outside it a bare digit does nothing. It used to take a step out of
        the merge, which put in and out one shift apart on the same key for a
        decision that is worth a deliberate press.
        """
        if self.aligning:
            self.aim_step(n)

    def reset_offset_one(self):
        """Put the selected frame back where the camera took it.

        The pair `⇧K` makes: that one clears the whole set, this one clears
        the frame the arrows are moving. Without it, changing your mind about
        one frame of five meant counting presses backwards, pressing ⌘Z until
        something else came back with it, or clearing all five.
        """
        f = self.frame
        if self.aim is None or self.aim >= len(f["steps"]):
            return self.note("click a frame's offset first")
        step = f["steps"][self.aim]
        if not any(step.get("offset") or ()):
            return self.note("%s is where the camera took it" % step["frame"])
        self._remember()
        for gone in ("offset", "offset_peak", "offset_sure"):
            step.pop(gone, None)
        plan.mark(f, "steps")
        self.note("%s put back where the camera took it" % step["frame"])
        self._draw_soon(0)

    def reset_offsets(self):
        """Put every exposure of this bracket back where it was shot.

        The full reset is out of reach while the mode is on -- it changes the
        radius and the exposures too, which is not what somebody lining up a
        limb is asking for. This one undoes exactly the offsets, the frame's
        own included, and nothing else.
        """
        f = self.frame
        self._remember()
        moved = sum(1 for step in f["steps"] if any(step.get("offset") or (0, 0)))
        for step in f["steps"]:
            for gone in ("offset", "offset_peak", "offset_sure"):
                step.pop(gone, None)
        if any(f.get("offset") or (0, 0)):
            moved += 1
        f.pop("offset", None)
        plan.mark(f, "steps")
        self.note("%d offset%s cleared" % (moved, "" if moved == 1 else "s"))
        self._fill_steps(f)
        self._read()
        self._paint()
        self.reload_list()

    def _blend(self):
        """x switches between the two overlays: edges, and the frames themselves.

        It used to take the overlay off and show the merge as it stands, which
        is `eclipse-aligner`'s job for the same key -- but there the merge is
        the thing being aligned, and here it is a picture made *before* any of
        this, from the offsets you are in the middle of changing. Alignment
        mode showing the state you have just left is worse than useless: it
        looks like a preview of the result and it is not one.

        So the key now moves between the two ways of seeing the same frames,
        which is what it is for. What the merge would look like is not
        knowable from here anyway -- HDRMerge decides that, and it is one
        Return away.
        """
        if not self.aligning:
            return
        self.overlay = "layers" if self.overlay == "edges" else "edges"
        self.view = 4
        self._paint()
        self.note("the frames themselves, each in its own colour"
                  if self.overlay == "layers" else "the edges of each frame")

    def busy_aligning(self):
        """True, and says so, when a command has to wait for the mode to end."""
        if self.aligning:
            self.note("aligning set %d — ↩ applies it, esc puts it back"
                      % self.frame["index"])
            return True
        return False

    def step_row(self, step):
        """One row up or down the list, whatever has the focus."""
        if self.busy_aligning():
            return
        if self.i in self.rows:
            row = self.rows.index(self.i) + step
            if 0 <= row < len(self.rows):
                self.show_frame(self.rows[row])

    def nudge_exposure(self, tenths):
        """Half a stop up or down, from the keys. Bound to [ and ]."""
        self.slider.setValue(max(-80, min(80, self.slider.value() + tenths)))

    # The outputs offered, each a finished answer: what it says, what it is
    # for, and what it turns into.
    #
    # Two things here are worth stating rather than implying. HDRMerge writes
    # only **floating-point** DNGs -- its 16 is a half float, its 24 the DNG
    # spec's FP24 -- so the ordinary 16-bit integer DNG that every raw editor
    # opens is written by this program instead, from the merge. And the
    # tone-mapped TIFFs carry **this window's preview curve**, which is a log
    # stretch and a 1/1.6 gamma; it is not sRGB and is not any standard, and
    # the point of it is that the file matches the picture you judged.
    #
    # FP24 is missing on purpose: tifffile cannot decode it, so this program
    # could not reopen its own output.
    # Each one is (label, paragraphs, options). Paragraphs, because these are
    # three answers and not one sentence: what the file is, what it costs, and
    # what whatever opens it has to be told. The third was the one missing,
    # and it is not about any particular program: a scene-linear file opened
    # without being told it is scene-linear comes up looking wrong anywhere,
    # and what fixes it is two facts -- primaries and gamma.
    # **The first one is what somebody who does not choose gets**, and it is
    # the float EXR because the 16-bit raw cannot be trusted with this
    # material. A linear integer is a poor container for a merge that spans
    # 19 stops: it spends 32768 of its codes on the first stop and has four
    # left by the thirteenth. Measured on frame 210 of the eclipse run, in a
    # 400x400 patch of veil 13.3 stops below white: the float merge holds
    # 1417 distinct values there, the 16-bit linear raw holds **four**, and
    # one step is 15.8 % of the level. That is visible banding, and it is
    # visible because the noise in that region is a fifth of the step -- too
    # small to dither it. Elsewhere the same encoding is fine, which is how
    # it passed: on frames whose shadows are photon-noisy the noise is 2.7 to
    # 270 times the step and nothing can band. A default has to be right on
    # the bad frame.
    OUTPUTS = (
        ("EXR — 32-bit float, Rec.709 linear",
         ("The measurement itself: values proportional to light, with no "
          "display curve and no clipping.",
          "265 MB a frame at 24 MP.",
          "Interpret: Rec.709 primaries, linear gamma. Whatever opens it has "
          "to be told both, because the file carries no chromaticities -- it "
          "cannot be read off it."),
         {"format": "exr", "bits": 32}),
        ("EXR — 16-bit half float, Rec.709 linear",
         ("The same, half the size, and enough for anything that will be "
          "graded. The deepest shadows quantise, which on this material is "
          "the corona far from the limb.",
          "82 MB a frame.",
          "Interpret: Rec.709 primaries, linear gamma, exactly as the "
          "32-bit."),
         {"format": "exr", "bits": 16}),
        ("DNG — 16-bit integer, lossless JPEG",
         ("An ordinary integer raw: the merged mosaic quantised linearly from "
          "black to white. DaVinci Resolve reads it with its raw controls. "
          "Nothing is lost at the top -- the merge is already clipped at the "
          "white level -- and what goes at the bottom is the part of the float "
          "range below one code value of the original.",
          "27 MB a frame, the mosaic in lossless JPEG: the pixels are "
          "identical to the uncompressed file, byte for byte. The smallest "
          "thing here by a distance -- a third of the 16-bit EXR -- and it "
          "keeps the raw controls.",
          "Interpret: nothing to set. It is a raw, and a raw editor reads its "
          "colour matrix and its white balance out of the file -- put back "
          "here from the camera's own, since HDRMerge mislabels them.",
          "Where it fails: a linear integer cannot hold a merge that spans "
          "19 stops. Twelve stops below white a step is already a per cent "
          "of the level, and on frame 210 of the eclipse run a smooth patch "
          "of veil 13 stops down came out with four distinct values against "
          "the float's 1417 -- banding you can see. Use it where the picture "
          "lives in the top ten stops; use the EXR where it does not."),
         {"format": "dng16", "bits": 16}),
        ("DNG — 32-bit float, deflate",
         ("The merged mosaic exactly as HDRMerge left it: same pixels, same "
          "tags, nothing restored and nothing corrected. What this program "
          "puts right in the 16-bit one, here it deliberately does not.",
          "47 MB a frame -- a fifth of the 32-bit EXR, because a mosaic is "
          "one value per pixel and a developed picture is three.",
          "Interpret: as a raw, but only where float DNG is really read -- "
          "Adobe, RawTherapee, darktable. Anything built on LibRaw decodes it "
          "near-black, about two levels. And HDRMerge leaves the colour "
          "calibration mislabelled: it keeps the tungsten matrix and calls it "
          "daylight, so a developer applies the wrong one and the picture "
          "comes out green. That is the price of untouched; the 16-bit above "
          "puts the camera's tags back."),
         {"format": "dng", "bits": 32}),
        ("TIFF — 16-bit sRGB",
         ("Developed and encoded with the standard sRGB transfer. No log "
          "stretch, no local contrast, and nothing you move in this window: "
          "what you look at is for judging the merge, not for deciding the "
          "file. The reference is measured once from the run itself and used "
          "for every frame -- a level measured per frame is flicker.",
          "About 130 MB a frame on this material; gamma encoding spreads the "
          "values, so it compresses worse than linear data.",
          "Interpret: sRGB, the transfer and the primaries both. It is the "
          "one file here a colour-managed program reads without being told "
          "anything."),
         {"format": "tiff", "bits": 16, "curve": "srgb"}),
        ("TIFF — 8-bit sRGB",
         ("The same at a quarter of the size, for a proof or a contact sheet. "
          "Eight bits of an eclipse is a picture of it, not the data.",
          "About 36 MB a frame.",
          "Interpret: as the 16-bit, with less room to grade it."),
         {"format": "tiff", "bits": 8, "curve": "srgb"}),
    )

    def process(self):
        """Merge and develop the whole plan, after asking what should come out.

        The output is the timelapse, not the brackets: every frame the plan
        keeps, in order, with each bracket replaced by its merge. What the
        merges become, and what happens to the frames that had nothing to
        merge, are the three things worth asking about -- and the defaults are
        the ones this material wants, so the dialog is mostly a place to read
        them.
        """
        if self.busy_aligning():
            return
        QtCore, _, QtWidgets = _widgets()

        if self.dirty:
            self.save()
        keeps = [f for f in self.doc["frames"] if f["include"]]
        merges = [f for f in keeps if len(plan.merged_steps(f)) > 1]
        singles = len(keeps) - len(merges)
        from .apply import OUT_DIR

        out_dir = os.path.join(self.folder, OUT_DIR)

        box = QtWidgets.QDialog(self.win)
        box.setWindowTitle("Process %s" % os.path.basename(self.folder))
        v = QtWidgets.QVBoxLayout(box)
        v.setContentsMargins(22, 18, 22, 16)
        v.setSpacing(12)
        head = QtWidgets.QLabel(
            '<div style="font-size:15px"><b>%d frames out</b></div>'
            '<div style="color:#8a8a90;margin-top:3px">%d merges and %d single '
            'frames, in capture order. The raws are read and never written.'
            '</div>' % (len(keeps), len(merges), singles))
        head.setTextFormat(QtCore.Qt.RichText)
        v.addWidget(head)

        where = QtWidgets.QHBoxLayout()
        where.setSpacing(8)
        where.addWidget(self._lab("into", "#8a8a90"))
        chosen = [out_dir]
        path = self._lab(out_dir, "#e8e8ea", 11)
        path.setToolTip(_tip("Everything this writes goes here. Nothing is ever "
                        "written beside the raws except the plan and the "
                        "preview cache."))
        where.addWidget(path, 1)
        pick = QtWidgets.QPushButton("Choose…")

        def choose():
            got = QtWidgets.QFileDialog.getExistingDirectory(
                box, "Where the processed sequence goes", chosen[0])
            if got:
                chosen[0] = got
                path.setText(got)

        pick.clicked.connect(choose)
        where.addWidget(pick)
        v.addLayout(where)

        # One list of finished answers rather than two questions to combine.
        # Format and depth are not independent in practice -- there is no
        # 24-bit EXR and a 16-bit merged DNG posterises the shadows this
        # material is made of -- so the pairs that make sense are named.
        form = QtWidgets.QHBoxLayout()
        form.setSpacing(10)
        form.addWidget(self._lab("as", "#8a8a90"))
        out = QtWidgets.QComboBox()
        for label, paras, _what in self.OUTPUTS:
            out.addItem(label)
            # Rendered here, not stored raw: the paragraphs are a tuple, and a
            # tuple handed to ToolTipRole is a tooltip that never appears --
            # which is how the list lost the explanations it is there for.
            out.setItemData(out.count() - 1, _paras(*paras),
                            QtCore.Qt.ToolTipRole)
        out.setFixedWidth(330)
        out.currentIndexChanged.connect(
            lambda i: out.setToolTip(_paras(*self.OUTPUTS[i][1])))
        out.setToolTip(_paras(*self.OUTPUTS[0][1]))
        form.addWidget(out)
        form.addStretch(1)
        v.addLayout(form)

        keep = QtWidgets.QCheckBox(
            "write the frames that have no bracket (%d of them)" % singles)
        keep.setChecked(True)
        keep.setToolTip(_tip("Off, the output holds only the merges and the "
                        "timelapse has holes in it where the single frames "
                        "were."))
        v.addWidget(keep)
        convert = QtWidgets.QCheckBox("convert those to the output format too")
        convert.setToolTip(_paras(
            "Off, the single frames are copied exactly as they came out of "
            "the camera, keeping their own format. Nothing is re-developed "
            "that nobody asked to change, and it costs seconds.",
            "On, they are developed like the merges, so the output is one "
            "sequence of one kind of file. Worth it when what reads the "
            "folder wants that — a folder of EXRs with raws among them is "
            "read as the EXRs alone — and it costs the time and the disk of "
            "developing every frame rather than the merges."))
        v.addWidget(convert)
        keep.toggled.connect(convert.setEnabled)

        note = self._lab("", "#70737c", 11)
        note.setWordWrap(True)
        v.addWidget(note)

        def describe():
            want = self.OUTPUTS[out.currentIndex()][2]
            fmt, bits = want["format"], want["bits"]
            single_out = ("copied unchanged" if not convert.isChecked()
                          else "developed like the merges")
            note.setText(
                "%d %s and %s. It takes minutes to hours; the window stays "
                "open and the terminal reports."
                % (len(merges),
                   "merges developed to %s-bit EXR" % bits if fmt == "exr"
                   else "merges written as %s-bit DNG" % bits
                   if fmt in ("dng", "dng16")
                   else "merges written as %s-bit %s TIFF"
                        % (bits, want.get("curve", "linear")),
                   ("%d single frames %s" % (singles, single_out))
                   if keep.isChecked() else "no single frames"))

        out.currentIndexChanged.connect(lambda *_: describe())
        for widget in (keep, convert):
            widget.toggled.connect(lambda *_: describe())
        describe()

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Cancel | QtWidgets.QDialogButtonBox.Ok)
        buttons.button(QtWidgets.QDialogButtonBox.Ok).setText("Process")
        buttons.accepted.connect(box.accept)
        buttons.rejected.connect(box.reject)
        v.addWidget(buttons)
        centre(box, self.win)
        if box.exec() != QtWidgets.QDialog.Accepted:
            return

        import subprocess
        import sys

        want = self.OUTPUTS[out.currentIndex()][2]
        fmt, bits = want["format"], want["bits"]
        cmd = self_command() + ["apply", self.path, "-o", chosen[0],
                                "--format", fmt]
        if fmt == "dng":
            cmd += ["--bps", str(bits)]
        elif fmt == "dng16":
            cmd += ["--bps", "32"]      # the float merge is the input to it
        else:
            # The merged DNG in the middle stays full float whatever comes out
            # of it: it is an intermediate, and there is nothing to gain by
            # quantising it twice.
            cmd += ["--exr-bits", str(bits), "--bps", "32"]
        if fmt == "tiff":
            # The curve and nothing else. What the window is showing -- the
            # exposure, the local contrast, the reference measured off
            # whichever frame is on screen -- decides how the merge is judged
            # here, and has no business deciding the file: `apply` measures
            # its own reference from the run.
            cmd += ["--tiff-curve", want["curve"]]
        if not keep.isChecked():
            cmd.append("--no-unmerged")
        elif convert.isChecked():
            cmd.append("--convert-unmerged")
        # What the bar will count: every merge, and every frame that gets
        # developed -- which is none of them for a DNG, because a DNG is the
        # merge itself, and the singles only when they are being converted
        # rather than copied through.
        develops = 0
        if fmt in ("exr", "tiff"):
            develops = len(merges) + (singles if keep.isChecked()
                                      and convert.isChecked() else 0)
        self._watch_job(cmd, out_dir, len(merges), develops)

    def _watch_job(self, cmd, out_dir, merges, frames):
        """Run `apply` and show what it is doing, in its own window.

        It used to start the subprocess and say "it reports in the terminal",
        which is true of a command and meaningless of an icon in the Dock:
        there is no terminal, and the longest thing the program does was the
        one thing with nothing to look at.

        What it reads is `apply`'s own progress -- `merge 3/20`, `develop
        5/20`, written to stderr with a carriage return. Python's universal
        newlines turn that return into a line ending, so the reader gets one
        line per step without the subprocess having to be changed.

        Work is counted in tasks and not in seconds: a merge and a develop are
        within a factor of two of each other at full size, and pretending to
        know the ratio would make the bar lie in a different way. The estimate
        is held steady the same way the first pass holds it.
        """
        QtCore, _, QtWidgets = _widgets()
        import subprocess
        import time

        total = merges + frames
        win = QtWidgets.QWidget()
        win.setWindowTitle("hdrmerge-timelapser")
        win.setFixedWidth(560)
        v = QtWidgets.QVBoxLayout(win)
        v.setContentsMargins(28, 24, 28, 24)
        v.setSpacing(12)
        head = QtWidgets.QLabel(
            '<div style="font-size:15px"><b>Processing…</b></div>'
            '<div style="color:#8a8a90;margin-top:3px">%d merges and %d '
            'frames to develop, into %s</div>'
            % (merges, frames, os.path.basename(out_dir)))
        head.setTextFormat(QtCore.Qt.RichText)
        v.addWidget(head)
        bar = QtWidgets.QProgressBar()
        bar.setRange(0, total)
        v.addWidget(bar)
        line = self._lab("starting…", "#70737c")
        v.addWidget(line)
        buttons = QtWidgets.QHBoxLayout()
        buttons.addStretch(1)
        stop = QtWidgets.QPushButton("Stop")
        buttons.addWidget(stop)
        v.addLayout(buttons)
        centre(win, self.win)
        win.show()

        state = {"merge": 0, "develop": 0, "started": time.time(),
                 "said": None, "shown": ""}

        def eta():
            done = state["merge"] + state["develop"]
            left = total - done
            if not left or not done:
                return ""
            seconds = left * (time.time() - state["started"]) / done
            now = ("under a minute" if seconds < 45
                   else "about %d min" % round(seconds / 60))
            was = state["said"]
            if was is None or abs(seconds - was) > 0.2 * was or seconds < 45:
                state["said"], state["shown"] = seconds, now
            return "  ·  %s left" % state["shown"]

        def say():
            bits = ["%d of %d merged" % (state["merge"], merges)]
            if frames:
                bits.append("%d of %d developed" % (state["develop"], frames))
            line.setText("  ·  ".join(bits) + eta())
            bar.setValue(state["merge"] + state["develop"])

        class Signals(QtCore.QObject):
            step = QtCore.Signal(str, int)
            done = QtCore.Signal(int)

        signals = Signals()

        def advance(stage, i):
            state[stage] = i
            say()

        def finished(code):
            bar.setValue(total)
            stop.setText("Close")
            line.setText("done — %d frames in %s"
                         % (state["develop"] or state["merge"],
                            os.path.basename(out_dir))
                         if code == 0 else
                         "stopped" if code < 0 else
                         "something failed — exit %d" % code)
            self.note("processing finished" if code == 0 else
                      "processing stopped" if code < 0 else
                      "processing failed — exit %d" % code)

        signals.step.connect(advance)
        signals.done.connect(finished)

        proc = subprocess.Popen(cmd, stderr=subprocess.PIPE,
                                stdout=subprocess.DEVNULL, text=True,
                                **preview.hidden_kwargs())

        class Job(QtCore.QRunnable):
            def run(self):
                import re
                for raw in proc.stderr:
                    got = re.search(r"(merge|develop) (\d+)/(\d+)", raw)
                    if got:
                        signals.step.emit(got.group(1), int(got.group(2)))
                signals.done.emit(proc.wait())

        QtCore.QThreadPool.globalInstance().start(Job())

        def pressed():
            if proc.poll() is None:
                proc.terminate()
                line.setText("stopping…")
            else:
                win.close()
        stop.clicked.connect(pressed)
        # Closing the window is not stopping the job: the merges already
        # written stay written, and a job of an hour should not end because
        # somebody wanted the window back.
        win.closeEvent = lambda e: e.accept()
        self._job_window = win          # keep it alive
        self.note("processing started")

    def show_step(self, n):
        """Put one step of the bracket in the big view, to look at it whole."""
        from .detect import shutter

        f = self.frame
        if not (0 <= n < len(f["steps"])):
            return
        path = self.sources.get(f["steps"][n]["frame"])
        if not path:
            return
        shot = read_shot(path)
        self.merged = None
        self._read("frame %s on its own · %s"
                   % (shutter(shot.exp), f["steps"][n]["frame"]))
        self.image.setText("developing…")
        self._async_view(path)

    def _measure_again(self):
        """Measure the folder again, keeping every correction made by hand.

        The only way in is the banner that appears when the folder has changed
        since it was measured -- which is the only moment it means anything.
        It was on the command sheet for a while and nobody could say what it
        was for, because from there it was an answer to a question the window
        had not asked.
        """
        if self.busy_aligning():
            return
        from . import detect as detect_cmd

        self.note("measuring again…")
        _, _, QtWidgets = _widgets()
        QtWidgets.QApplication.processEvents()
        fresh = detect_cmd.detect(self.folder, self.doc.get("settings"),
                                  report=lambda *_: None)
        orphans = plan.carry_over(fresh, self.doc)
        self.doc = fresh
        self.dirty = True
        self.i = min(self.i, len(fresh["frames"]) - 1)
        self.reload_list()
        self.show_frame(self.i)
        self.note("measured again; %d corrections carried over%s"
                  % (sum(1 for f in fresh["frames"] if f.get("manual")),
                     ", %d could not be placed" % len(orphans) if orphans else ""))

    def _closing(self, event):
        """Do not let the window take unsaved corrections with it.

        The merges survive a restart -- they are files -- but the plan only
        does if it was written, and an afternoon of decisions living in a
        window is an afternoon one keystroke from gone.
        """
        _, _, QtWidgets = _widgets()

        if not self.dirty:
            return event.accept()
        box = QtWidgets.QMessageBox(self.win)
        box.setWindowTitle("hdrmerge-timelapser")
        box.setText("This plan has changes that are not saved.")
        box.setInformativeText("The merges stay in the cache either way; the "
                               "corrections do not.")
        box.setStandardButtons(QtWidgets.QMessageBox.Save
                               | QtWidgets.QMessageBox.Discard
                               | QtWidgets.QMessageBox.Cancel)
        box.setDefaultButton(QtWidgets.QMessageBox.Save)
        centre(box, self.win)
        answer = box.exec()
        if answer == QtWidgets.QMessageBox.Cancel:
            return event.ignore()
        if answer == QtWidgets.QMessageBox.Save:
            self.save()
        event.accept()

    def start_over(self):
        """Reset the folder: throw away everything measured about it.

        The same thing `hdrmerge-timelapser reset` does, from the window: the
        plan and the previews go, the raws do not, and the folder opens as it
        would the first time. It asks first and it counts what it is about to
        throw away, because the previews are only slow to make again and the
        corrections are not recoverable at all.
        """
        _, _, QtWidgets = _widgets()
        from . import preview as pv
        from .reset import reset

        edited = sum(1 for f in self.doc["frames"] if f.get("manual"))
        size = sum(os.path.getsize(os.path.join(pv.cache_dir(self.folder), n))
                   for n in os.listdir(pv.cache_dir(self.folder))
                   if os.path.isfile(os.path.join(pv.cache_dir(self.folder), n))
                   ) if os.path.isdir(pv.cache_dir(self.folder)) else 0
        box = QtWidgets.QMessageBox(self.win)
        box.setWindowTitle("hdrmerge-timelapser")
        box.setText("Reset %s?" % os.path.basename(self.folder))
        box.setInformativeText(
            "This removes the plan%s and %.0f MB of previews, then measures "
            "and merges the folder as if it had never been opened. The raw "
            "frames are not touched."
            % (" — including %d sets you edited by hand" % edited
               if edited else "", size / 1e6))
        box.setStandardButtons(QtWidgets.QMessageBox.Cancel
                               | QtWidgets.QMessageBox.Yes)
        box.setDefaultButton(QtWidgets.QMessageBox.Cancel)
        centre(box, self.win)
        if box.exec() != QtWidgets.QMessageBox.Yes:
            return
        reset(self.folder)
        self.dirty = False          # nothing left to save it to
        self.restart = True
        self.win.close()

    def save(self):
        plan.save(self.doc, self.path)
        self.dirty = False
        self.note("saved to %s" % self.path)
        self.reload_list()


def first_pass(folder, doc, sources, wb, trimmed=0):
    """Merge the plan once, with a progress bar, before anyone edits anything.

    A merge is 2.7 s at r3 and 23 s at r250 here, so the first look at any frame
    would otherwise be a wait, and the window would spend its first minutes
    feeling broken. This does them all up front and says how long it will take,
    which is the difference between waiting and not knowing.

    It is a *first* pass and not a rendering: everything lands in the same cache
    the editor reads, keyed by the steps and the radius, so a frame stays warm
    until you change it -- and the frame you do change is the one merge you then
    wait for, alone. Anything already cached is skipped, so opening the folder
    again goes straight in.

    Returns True if it ran to the end; False if the window was closed, which
    means give up rather than open the editor half warmed.
    """
    QtCore, _, QtWidgets = _widgets()
    import time

    from .apply import resolve_hdrmerge

    # **Only the brackets.** Warming the single frames too was three quarters
    # of this: measured on the real folder, 20 brackets at ~12 s of merging is
    # 4 minutes, and 427 single frames at 1.8 s of developing is 12.5 more --
    # spent on frames that merge nothing, decide nothing and are hidden in the
    # list by default. One of them costs 1.8 s the moment you actually look at
    # it, which is the right time to pay for it.
    todo, skipped, singles_left = [], 0, 0
    cam2out = preview.colour(folder, next(iter(sources.values())))
    for f in doc["frames"]:
        steps = plan.merged_steps(f)
        radius = (f.get("blend_radius") or {}).get("chosen") or 3
        if len(steps) < 2:
            singles_left += 1
            continue
        if preview.cached_merge(folder, f, radius, width=None):
            skipped += 1
        else:
            todo.append((f, radius))
    if not todo:
        return True
    merges = sum(1 for _, r in todo if r is not None)
    singles = len(todo) - merges
    # The bar measures work, not items. A merge is 10-20 s and developing a
    # single frame is about 1, so counting them alike made the bar crawl
    # through the merges and then leap: it looked unstable because it was
    # reporting the wrong thing. Ten to one is roughly what they cost.
    WORK_MERGE, WORK_SINGLE = 10, 1
    total_work = merges * WORK_MERGE + singles * WORK_SINGLE

    app = _app()
    win = QtWidgets.QWidget()
    win.setWindowTitle("hdrmerge-timelapser")
    win.setFixedWidth(620)
    v = QtWidgets.QVBoxLayout(win)
    v.setContentsMargins(28, 24, 28, 24)
    v.setSpacing(12)
    head = QtWidgets.QLabel(
        '<div style="font-size:15px"><b>Initializing…</b></div>'
        '<div style="color:#8a8a90;margin-top:3px">Processing HDR images for '
        'the first time%s</div>'
        % ("" if not trimmed else
           " — the cache was over its limit, so %d old previews were dropped "
           "and are being made again" % trimmed))
    head.setTextFormat(QtCore.Qt.RichText)
    # It must not be the one that gives way. Wrapping the tally below made the
    # layout ask somebody for room, and what it took was the second line of
    # this heading -- cut in half, behind the bar.
    head.setMinimumHeight(head.sizeHint().height())
    v.addWidget(head)
    bar = QtWidgets.QProgressBar()
    bar.setRange(0, total_work)
    v.addWidget(bar)
    def counted():
        """What is made, what is left, and what was already there.

        Counted per kind. One number for both meant a single frame finishing
        advanced the tally of *HDR merges*, so the line said five merges were
        done when none was.
        """
        parts = []
        if merges:
            parts.append("%d of %d HDR merges" % (state["merges"], merges))
        if singles:
            parts.append("%d of %d single frames"
                         % (state["singles"], singles))
        parts.append("%d pending" % (len(todo) - state["done"]))
        if skipped:
            parts.append("%d skipped, already made" % skipped)
        if singles_left:
            parts.append("%d single frames left for later" % singles_left)
        return "  ·  ".join(parts)

    line = QtWidgets.QLabel()
    line.setStyleSheet("color:#70737c;")
    # Wrapped, because the tally grows a part at a time -- merges, singles,
    # pending, skipped, an estimate -- and on a folder with all of them it ran
    # off the right edge and the last one was cut in half. Two lines of room
    # from the start, so the window is the size it will need rather than the
    # size the first short line asks for.
    line.setWordWrap(True)
    line.setMinimumHeight(2 * line.fontMetrics().height())
    v.addWidget(line)

    def say(text):
        """Put the tally up, and ask for the room it actually needs.

        Two lines is the floor rather than the answer: the parts grow with the
        folder -- five of them, with numbers that reach the hundreds -- and a
        fixed reservation is a guess that a bigger run gets to disprove by
        cutting the last line in half.
        """
        line.setText(text)
        line.setMinimumHeight(max(2 * line.fontMetrics().height(),
                                  line.heightForWidth(line.width())))
    centre(win)
    win.show()

    state = {"done": 0, "merges": 0, "singles": 0, "work": 0,
             "stop": False, "started": time.time(), "said": None}

    class Signals(QtCore.QObject):
        one = QtCore.Signal(bool)

    signals = Signals()

    def eta():
        """Time left, from work done rather than items, and held steady.

        Two workers finish in pairs, so an estimate recomputed at every
        completion swung between five minutes and two and back. This one is
        measured in units of work and only changes what it says when the answer
        moves by more than a fifth -- an estimate that keeps changing is not
        one, and the number is rounded to what it can honestly claim anyway.
        """
        left = total_work - state["work"]
        if not left or not state["work"]:
            return ""
        seconds = left * (time.time() - state["started"]) / state["work"]
        now = ("under a minute" if seconds < 45
               else "about %d min" % round(seconds / 60))
        if state["said"] is None or seconds > 45:
            was = state["said"]
            if was is None or abs(seconds - was) > 0.2 * was:
                state["said"] = seconds
                state["shown"] = now
        else:
            state["said"], state["shown"] = seconds, now
        return "  ·  %s left" % state.get("shown", now)

    def advance(was_merge=True):
        state["done"] += 1
        state["merges" if was_merge else "singles"] += 1
        state["work"] += WORK_MERGE if was_merge else WORK_SINGLE
        bar.setValue(state["work"])
        say(counted() + eta())
        if state["done"] >= len(todo):
            win.close()

    say(counted())
    signals.one.connect(advance)

    # Closing the window abandons the whole run rather than opening the editor
    # on half a job: the point of the pass is that afterwards every frame is
    # there, and an editor that is instant on some frames and 23 s on others is
    # the state this was written to remove.
    win.closeEvent = lambda _e: state.update(stop=True)

    pool = QtCore.QThreadPool()
    pool.setMaxThreadCount(PREFETCH_JOBS)

    class Job(QtCore.QRunnable):
        def __init__(self, frame, radius):
            super().__init__()
            self.f, self.radius = frame, radius

        def run(self):
            if state["stop"]:
                return
            try:
                if self.radius is None:
                    # Exactly as the window will ask for it later: the cache key
                    # carries the width, so warming it at 560 while the window
                    # asks for it unsized filled the cache with pictures nobody
                    # would ever read, and every passthrough frame was still
                    # developed on first sight.
                    preview.thumb(folder, sources[plan.merged_steps(self.f)[0]],
                                  cam2out, wb=wb)
                else:
                    preview.merge_preview(folder, self.f, sources, self.radius,
                                          resolve_hdrmerge(None), cam2out,
                                          wb=wb)
            except Exception:                               # noqa: BLE001
                pass          # a frame that will not merge is the editor's news
            signals.one.emit(self.radius is not None)

    for frame, radius in todo:
        pool.start(Job(json.loads(json.dumps(frame)), radius))
    app.exec()
    state["stop"] = True
    pool.clear()
    return state["done"] >= len(todo)


def edit(folder, serve=None):
    """Open the editor on a folder's plan and block until the window closes."""
    QtCore, QtGui, QtWidgets = _widgets()
    app = _app()
    path = plan.path_for(folder)
    if not os.path.isfile(path):
        raise SystemExit("No plan in %s — run `detect` first." % folder)
    doc = plan.load(path)

    gone, freed = preview.prune(folder)
    if gone:
        print("  cache: %d old previews removed, %.0f MB freed"
              % (gone, freed / 1e6))

    sources = {n: os.path.join(folder, n)
               for n in sorted(os.listdir(folder))
               if os.path.splitext(n)[1].lower() in RAW_EXTS}
    if sources and not first_pass(folder, doc, sources,
                                  doc["settings"].get("exr_wb"), gone):
        return None

    editor = Editor(folder, doc, path)
    editor.show_frame(editor.rows[0] if editor.rows else 0)
    centre(editor.win)
    editor.win.show()
    if serve:
        from . import remote
        remote.serve(editor, serve)
    app.exec()
    # On the way out, not while it is being dragged: the size that matters is
    # the one the window was left at, and a resize handler would write the
    # store on every pixel of a drag.
    keep_size(editor.win, "editor")
    return "restart" if editor.restart else 0


def ask_radius(parent=None):
    """Which blur radius a folder should open with, asked once, before measuring.

    The measurement can choose one per frame, and it is worth having -- but it
    only ever *vetoes*: it scores what a wide mask would spoil and knows
    nothing about the halo a narrow one leaves. So where the short exposures
    carry signal everywhere it goes wide, which is right for a transition frame
    and surprising on everything else.

    It is not slower -- measured on a 525-frame folder, detect takes 11.8 s
    with the radius measured and 11.9 s with it fixed, because the cost is
    reading the pixels of every exposure to score its coverage, which happens
    either way. What it costs is corrections: on the real run, **16 of the 20
    bracketed frames had their radius changed by hand, every one of them
    downwards** -- auto said 100 or 250, and the answer was 3 nine times.

    So the default is 3, HDRMerge's own, and widening the frames that ask for
    it is left to a person. Not because it is better in principle: because
    that is what the record of an actual edit says.
    """
    QtCore, _, QtWidgets = _widgets()

    box = QtWidgets.QDialog(parent)
    box.setWindowTitle("hdrmerge-timelapser")
    # Fixed, and the text wrapped inside it. A paragraph handed to Qt as one
    # line is a dialog as wide as the paragraph -- this one came out 1400 px
    # across and three lines tall, which is a shape no dialog should have.
    box.setFixedWidth(560)
    v = QtWidgets.QVBoxLayout(box)
    v.setContentsMargins(22, 18, 22, 16)
    v.setSpacing(12)
    head = QtWidgets.QLabel(
        '<div style="font-size:15px"><b>Default HDRMerge blur radius</b></div>'
        '<div style="color:#8a8a90;margin-top:4px">How far the merge spreads '
        'the boundary between one exposure and the next. One radius for the '
        'whole folder, or one measured for each bracketed set. Either can be '
        'changed afterwards, on a set or on a selection.</div>')
    head.setTextFormat(QtCore.Qt.RichText)
    head.setWordWrap(True)
    v.addWidget(head)
    pick = QtWidgets.QComboBox()
    # `auto` first and chosen: it is the answer for a bracket that jumps, and
    # it is now undoable per set from the window, so picking it commits to
    # nothing. The fixed radii carry no tooltip -- a number is a number.
    pick.addItem("auto (detected per bracketed set)")
    pick.setItemData(0, _paras(
        "Finds the best radius for each bracketed set, and removes the frames "
        "that are irrelevant to the output.",
        "Takes the widest blur radius that keeps the part it spoils under "
        "0.1 % of the picture. It only widens the sets whose bracket step is "
        "over 2 EV, where a heavy bracket would otherwise leave artefacts "
        "around bright edges."), QtCore.Qt.ToolTipRole)
    pick.addItem("3 (HDRMerge default)")
    for r in RADIUS_CANDIDATES[1:]:
        pick.addItem(str(r))
    v.addWidget(pick)
    buttons = QtWidgets.QDialogButtonBox(
        QtWidgets.QDialogButtonBox.Cancel | QtWidgets.QDialogButtonBox.Ok)
    buttons.button(QtWidgets.QDialogButtonBox.Ok).setText("Measure")
    buttons.accepted.connect(box.accept)
    buttons.rejected.connect(box.reject)
    v.addWidget(buttons)
    centre(box, parent)
    if box.exec() != QtWidgets.QDialog.Accepted:
        return None
    i = pick.currentIndex()
    return "auto" if i == 0 else RADIUS_CANDIDATES[i - 1]


def measuring(folder, radius=None):
    """Measure a folder with a window open, and return the plan.

    Opening a folder that has never been measured used to answer with an error
    telling the person to go and run another command. Measuring is what the
    program does with a folder; it should just do it, and say how it is going
    while it does -- eight seconds on ninety raws warm, longer when the disk has
    to fetch four gigabytes.
    """
    QtCore, _, QtWidgets = _widgets()
    from . import detect as detect_cmd

    app = _app()
    win = QtWidgets.QWidget()
    win.setWindowTitle("hdrmerge-timelapser")
    win.setFixedWidth(520)
    v = QtWidgets.QVBoxLayout(win)
    v.setContentsMargins(28, 24, 28, 24)
    v.setSpacing(12)
    head = QtWidgets.QLabel(
        '<div style="font-size:15px"><b>%s</b></div>'
        '<div style="color:#8a8a90;margin-top:3px">Reading every raw and '
        'cutting the sequence into bracketed sets.</div>'
        % os.path.basename(folder))
    head.setTextFormat(QtCore.Qt.RichText)
    v.addWidget(head)
    bar = QtWidgets.QProgressBar()
    bar.setRange(0, 0)
    v.addWidget(bar)
    line = QtWidgets.QLabel("reading the frames…")
    line.setStyleSheet("color:#70737c;")
    v.addWidget(line)
    centre(win)
    win.show()

    class Signals(QtCore.QObject):
        step = QtCore.Signal(int, int)
        done = QtCore.Signal(object, str)

    signals = Signals()
    signals.step.connect(lambda i, n: (bar.setRange(0, n), bar.setValue(i + 1),
                                       line.setText("bracket %d of %d" % (i + 1, n))))
    box = {}

    def finished(doc, error):
        box["doc"], box["error"] = doc, error
        win.close()

    signals.done.connect(finished)

    class Job(QtCore.QRunnable):
        def run(self):
            try:
                doc = detect_cmd.detect(
                    folder, {"blend_radius": radius,
                             "prune_empty": radius == "auto"} if radius else None,
                    progress=lambda i, n: signals.step.emit(i, n),
                    report=lambda *_: None)
            except Exception as e:                          # noqa: BLE001
                return signals.done.emit(None, str(e))
            signals.done.emit(doc, "")

    QtCore.QThreadPool.globalInstance().start(Job())
    app.exec()
    if box.get("error"):
        raise SystemExit(box["error"])
    return box.get("doc")


def open_folder(folder, serve=None):
    """Open a folder: measure it if it has never been measured, then edit it.

    One entry point for every way in -- the window's opening screen, `edit
    FOLDER`, an icon with a folder dropped on it -- so that all of them behave
    the same. It loops because `Reset All` throws the plan away and asks to
    be let in again through the same door, measuring included.
    """
    while True:
        path = plan.path_for(folder)
        if not os.path.isfile(path):
            radius = ask_radius()
            if radius is None:
                return 0
            doc = measuring(folder, radius)
            if doc is None:
                return 0
            plan.save(doc, path)
        answer = edit(folder, serve)
        if answer != "restart":
            return answer


def add_args(ap):
    """The options of `hdrmerge-timelapser edit`."""
    ap.add_argument("folder", nargs="?",
                    help="The folder of frames whose plan to edit. Asked for "
                         "if skipped, which is how the window opens when it "
                         "is launched from the Finder with nothing to go on.")
    ap.add_argument("--serve", nargs="?", type=int, const=8765, default=None,
                    metavar="PORT",
                    help="Open a control socket on 127.0.0.1 (default port "
                         "8765) so the window can be driven and photographed "
                         "from outside. For reproducing a problem with "
                         "somebody, not for daily use.")


def run(args):
    folder = args.folder
    if folder is None:
        folder = ask()
        if folder is None:
            return 0
    remember(folder)
    return open_folder(folder, args.serve)


def main():
    """`hdrmerge-timelapser` with no arguments: ask for a folder, then work."""
    try:
        _widgets()
    except ImportError:
        print("The window needs the gui extra:  uv sync --extra gui\n"
              "Or use the command line: hdrmerge-timelapser detect FOLDER",
              file=sys.stderr)
        return 2
    folder = ask()
    if folder is None:
        return 0
    remember(folder)
    return open_folder(folder)
