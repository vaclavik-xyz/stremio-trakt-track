"""Test isolation: import this module before any project module.

Points TRAKT_TRACKER_HOME at a fresh temporary directory, so that no test can
read or write the real config, database, caches or journals.
"""
from __future__ import annotations

import atexit
import os
import pathlib
import shutil
import sys
import tempfile

CODE_DIR = pathlib.Path(__file__).resolve().parent.parent
TMP_HOME = pathlib.Path(tempfile.mkdtemp(prefix="trakt-tracker-test-"))
os.environ["TRAKT_TRACKER_HOME"] = str(TMP_HOME)
os.environ.pop("TRAKT_TRACKER_LOCK_FD", None)
atexit.register(shutil.rmtree, TMP_HOME, ignore_errors=True)
if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))

import common  # noqa: E402

assert common.DATA_DIR == TMP_HOME.resolve(), "tests must never use the real data directory"


def clean_home() -> None:
    """Remove everything a previous test left in the temporary data directory."""
    for p in TMP_HOME.iterdir():
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
