"""Frozen-application entry point.

PyInstaller needs a concrete script to start from. This is deliberately tiny:
the app lives in `raggy.app` so the same code runs from source (`./run.sh app`)
and from the packaged executable.
"""

import multiprocessing

from raggy.app import main

if __name__ == "__main__":
    # Harmless when unfrozen; required so any child process in a bundled build
    # re-executes the bundle instead of the system interpreter.
    multiprocessing.freeze_support()
    raise SystemExit(main())
