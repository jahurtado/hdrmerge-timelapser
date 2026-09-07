#!/usr/bin/env python3
"""Throw away everything this program has worked out about a folder.

    hdrmerge-timelapser reset FOLDER

Two things get removed: the plan, which is every decision `detect` measured and
every correction made by hand, and the preview cache, which is only expensive.
Nothing that came out of the camera is touched -- the raws are read and never
written, here or anywhere else in this program.

It exists because "start again from nothing" is otherwise a thing you do by
guessing which hidden directory to delete, and because a plan that was measured
by an older build carries older numbers: on a ladder of four radii where today
there are eight, or before a preview kept its layer map. Re-measuring is the
honest way to get the current answer rather than a mixture of two.
"""

import os

from . import plan, preview


def reset(folder, remove_plan=True):
    """Remove the cache, and the plan unless told otherwise. Returns a summary."""
    gone, freed = preview.clear(folder)
    path = plan.path_for(folder)
    had_plan = remove_plan and os.path.isfile(path)
    if had_plan:
        os.remove(path)
    return {"previews": gone, "bytes": freed, "plan": had_plan}


def add_args(ap):
    ap.add_argument("folder", help="The folder of raw frames to reset.")
    ap.add_argument("--keep-plan", action="store_true",
                    help="Throw away the previews but keep the plan, so the "
                         "corrections survive and only the pictures are made "
                         "again.")
    ap.add_argument("-y", "--yes", action="store_true",
                    help="Do not ask. The plan is not recoverable once gone.")


def run(args):
    folder = args.folder
    if not os.path.isdir(folder):
        raise SystemExit("%s is not a folder" % folder)
    path = plan.path_for(folder)
    edited = 0
    if os.path.isfile(path):
        try:
            edited = sum(1 for f in plan.load(path)["frames"] if f.get("manual"))
        except (OSError, ValueError):
            pass
    if not args.yes:
        what = ["the preview cache"]
        if not args.keep_plan and os.path.isfile(path):
            what.append("the plan%s"
                        % (" — including %d frames edited by hand" % edited
                           if edited else ""))
        print("About to remove from %s:" % folder)
        for line in what:
            print("  - %s" % line)
        if input("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
            return 0
    done = reset(folder, remove_plan=not args.keep_plan)
    print("  %d previews removed, %.0f MB freed%s"
          % (done["previews"], done["bytes"] / 1e6,
             ", plan removed" if done["plan"] else ""))
    print("Run `hdrmerge-timelapser edit %s` to measure it again." % folder)
    return 0
