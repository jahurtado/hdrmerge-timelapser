"""Entry point for the packaged application.

`cli.main` already treats a missing command as the editor and a bare path as a
folder to open, so a bundle is the same program with one extra chore: macOS
hands some launches a Process Serial Number, which is neither a command nor a
path and must not be taken for one.
"""

import multiprocessing
import sys

from hdrmerge_timelapser.cli import main

if __name__ == "__main__":
    # Nothing in the program starts a process pool any more -- the develop
    # runs on threads, exactly so that a frozen build stops re-executing its
    # own binary and filling the Dock with itself. This stays as the guard for
    # the day something does: without it, a re-executed worker opens a second
    # window instead of doing the work.
    multiprocessing.freeze_support()
    sys.exit(main([a for a in sys.argv[1:] if not a.startswith("-psn_")]))
