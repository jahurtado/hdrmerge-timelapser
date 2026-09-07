#!/usr/bin/env python3
"""One command, three subcommands, in the order they are meant to be run.

Run with nothing at all it opens the window, because an icon in the Dock arrives
with no arguments and a usage error is a poor answer to that.
"""

import argparse
import sys

from . import __version__


def identity():
    """(author, site) read from the package, so they are written once.

    The version already comes from `__init__` and the author from the project
    metadata, so nothing that shows them -- the terminal, the About box, the
    corner of a window -- carries a copy of its own to fall out of date. A
    frozen build may have no metadata to read; the constants in `editor` are
    what it falls back to.
    """
    from importlib import metadata
    try:
        m = metadata.metadata("hdrmerge-timelapser")
        who = m["Author"] or ", ".join(
            a.split("<")[0].strip() for a in m.get_all("Author-email") or [])
        site = ""
        for u in m.get_all("Project-URL") or []:
            if u.lower().startswith("homepage"):
                site = u.split(",", 1)[1].strip()
    except metadata.PackageNotFoundError:
        who, site = "", ""
    return who, site.replace("https://", "").replace("www.", "").rstrip("/")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv:
        from . import editor
        return editor.main()

    ap = argparse.ArgumentParser(
        prog="hdrmerge-timelapser",
        description="Plan and produce an HDR timelapse from bracketed bursts.")
    ap.add_argument("--version", action="version",
                    version="hdrmerge-timelapser %s" % __version__)
    subs = ap.add_subparsers(dest="command", required=True)

    from . import apply as apply_cmd
    from . import detect as detect_cmd
    from . import editor as edit_cmd
    from . import reset as reset_cmd

    for name, mod, help_text in (
            ("detect", detect_cmd, "measure a folder of raws and write the plan"),
            ("edit", edit_cmd, "open the plan in a window and correct it"),
            ("apply", apply_cmd, "merge and develop what the plan says"),
            ("reset", reset_cmd, "reset a folder: remove its plan and its "
                                 "previews, to start again")):
        sub = subs.add_parser(name, help=help_text, description=mod.__doc__,
                              formatter_class=argparse.RawDescriptionHelpFormatter)
        mod.add_args(sub)
        sub.set_defaults(run=mod.run)

    args = ap.parse_args(argv)
    try:
        return args.run(args)
    except ValueError as e:
        # What the library raises when a folder is not what it needs. It stays
        # a ValueError in there, because the window catches it and puts it in
        # a dialog; out here it is a message and an exit code, not a stack
        # trace about a folder somebody chose.
        raise SystemExit("%s" % e)
