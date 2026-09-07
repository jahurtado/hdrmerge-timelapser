"""`python -m hdrmerge_timelapser` -- the same entry point as the command.

The guard is not decoration, even now that `apply` develops on threads rather
than in a process pool. Where a pool worker exists at all it is, on Windows and
in a frozen build, a fresh interpreter that re-imports the main module to reach
the pickled callables; without `if __name__ == "__main__"` the bare `main()` at
import time would run in every one of them -- a second window, or a fork bomb.
It costs a line and it is the difference between a mistake and a disaster.
"""

import multiprocessing
import sys

from .cli import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
