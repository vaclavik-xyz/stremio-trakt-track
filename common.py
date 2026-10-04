"""Shared helpers: paths, settings, atomic writes, the run lock, caches, formatting."""
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import json
import os
import pathlib
import sys
import tempfile
import time

CODE_DIR = pathlib.Path(__file__).resolve().parent
# Credentials, the database, caches and journals live next to the code by default.
# TRAKT_TRACKER_HOME moves them elsewhere — the test suite relies on it so that
# tests can never touch real personal data.
DATA_DIR = pathlib.Path(os.environ.get("TRAKT_TRACKER_HOME") or CODE_DIR).expanduser().resolve()

# Exit codes shared by all commands. EXIT_WARN means "finished, but something
# needs a look" (unknown titles, rejected writes) — cron treats it as success
# for gating, but reports it.
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_REFUSED = 2
EXIT_LOCKED = 3
EXIT_GUARD = 4
EXIT_WARN = 10

DEFAULTS: dict = {
    "cache_ttl_hours": 24,        # Cinemeta / Trakt episode lists
    "match_threshold": 0.6,       # title similarity needed to pair episodes
    "repeat_guard_days": 7,       # same item written again within N days = anomaly
    "max_auto": 8,                # cron fills at most this many episodes per show
    "issue_remind_days": 7,       # cron repeats a lingering issue every N days
    "prune_max_rows": 50,         # sync refuses to drop more rows than this …
    "prune_max_fraction": 0.05,   # … or this share of the table
    "backup_dir": None,           # None = iCloud Drive
}


def settings() -> dict:
    """Defaults overridden by the optional "settings" block in config.json."""
    user: dict = {}
    path = DATA_DIR / "config.json"
    if path.exists():
        try:
            user = json.loads(path.read_text()).get("settings") or {}
        except (OSError, ValueError, AttributeError):
            user = {}
    return {**DEFAULTS, **{k: v for k, v in user.items() if k in DEFAULTS}}


# ------------------------------------------------------------------ files


def atomic_write_text(path: pathlib.Path, text: str, mode: int = 0o600) -> None:
    """Write via a temp file + os.replace: never a half-written file, never a
    moment with looser permissions than `mode`."""
    path = pathlib.Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def atomic_write_json(path: pathlib.Path, data, mode: int = 0o600, indent: int | None = 1) -> None:
    atomic_write_text(path, json.dumps(data, indent=indent, ensure_ascii=False, default=list), mode)


def read_json(path: pathlib.Path, default):
    try:
        return json.loads(pathlib.Path(path).read_text())
    except (OSError, ValueError):
        return default


class Journal:
    """A run journal saved after every change, so a crash mid-run still leaves
    an accurate record of what was already written."""

    def __init__(self, path: pathlib.Path, data: dict):
        self.path = path
        self.data = data
        self.save()

    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value):
        self.data[key] = value

    def save(self) -> None:
        atomic_write_json(self.path, self.data)


# ------------------------------------------------------------------- lock

LOCK_ENV = "TRAKT_TRACKER_LOCK_FD"


@contextlib.contextmanager
def lock(name: str = ".lock"):
    """Exclusive lock for anything that writes to Trakt.

    A parent (cron_daily) that already holds the lock passes the descriptor to its
    children via LOCK_ENV + pass_fds; flock on the same open file description
    succeeds, a fresh open of the file does not.
    """
    inherited = os.environ.get(LOCK_ENV)
    if inherited:
        try:
            fd = int(inherited)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (ValueError, OSError):
            pass
        else:
            yield fd
            return
    fd = os.open(DATA_DIR / name, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another run holds the lock (.lock) — try again later.", file=sys.stderr)
            sys.exit(EXIT_LOCKED)
        yield fd
    finally:
        os.close(fd)


# ------------------------------------------------------------------ cache


class JsonCache:
    """Small JSON cache with a TTL. Empty values are never stored: an empty answer
    is far more likely a hiccup than the truth, and caching it would hide a title
    for good."""

    def __init__(self, path: pathlib.Path, ttl_hours: float):
        self.path = pathlib.Path(path)
        self.ttl = ttl_hours * 3600

    def _load(self) -> dict:
        data = read_json(self.path, {})
        return data if isinstance(data, dict) else {}

    def get(self, key: str):
        entry = self._load().get(key)
        if not (isinstance(entry, dict) and "t" in entry and "v" in entry):
            return None             # missing, or written by an older version
        if time.time() - float(entry["t"]) >= self.ttl or not entry["v"]:
            return None
        return entry["v"]

    def put(self, key: str, value) -> None:
        if not value:
            return
        data = self._load()
        data[key] = {"t": time.time(), "v": value}
        atomic_write_json(self.path, data, indent=None)


# ------------------------------------------------------------- formatting


def cn(n: int, one: str, few: str, many: str) -> str:
    """Czech plural: 1 film, 2–4 filmy, 0 / 5+ filmů."""
    if n == 1:
        return f"{n} {one}"
    if 2 <= n <= 4:
        return f"{n} {few}"
    return f"{n} {many}"


def fmt_eps(pairs) -> str:
    """S2E1–E3, S3E1 — merges consecutive episodes per season."""
    by_season: dict[int, list[int]] = {}
    for s, e in pairs:
        by_season.setdefault(int(s), []).append(int(e))
    out = []
    for s in sorted(by_season):
        eps = sorted(set(by_season[s]))
        runs = []
        start = prev = eps[0]
        for e in eps[1:]:
            if e == prev + 1:
                prev = e
                continue
            runs.append((start, prev))
            start = prev = e
        runs.append((start, prev))
        for a, b in runs:
            if s == 0:
                out.append(f"speciál E{a}" if a == b else f"speciály E{a}–E{b}")
            else:
                out.append(f"S{s}E{a}" if a == b else f"S{s}E{a}–E{b}")
    return ", ".join(out)


def utc_bound(moment: dt.datetime) -> str:
    """A period boundary in the same shape Trakt stores `watched_at` in (UTC,
    no offset), so plain string comparison in SQLite is correct."""
    return moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
