"""A fake Trakt API for tests: records every call, answers from a routing table."""
from __future__ import annotations

import base64
import json
import zlib

import helpers  # noqa: F401
import track


class FakeTrakt:
    """Replaces track._req. `routes` maps (method, path) to a value, an exception,
    or a callable(body, params) returning a value."""

    def __init__(self, routes: dict | None = None):
        self.routes = dict(routes or {})
        self.calls: list[tuple[str, str, object]] = []

    def __call__(self, method, path, params=None, body=None, auth=True, cfg=None):
        self.calls.append((method, path, body))
        key = (method, path)
        if key not in self.routes:
            raise AssertionError(f"unexpected Trakt call {method} {path}")
        val = self.routes[key]
        if isinstance(val, Exception):
            raise val
        if callable(val):
            val = val(body, params)
        return val, {}

    def posts(self):
        return [(p, b) for m, p, b in self.calls if m == "POST"]


def history_ok(body, _params):
    """POST /sync/history answer: everything added."""
    movies = len(body.get("movies") or [])
    eps = sum(len(s["episodes"]) for sh in body.get("shows") or [] for s in sh["seasons"])
    return {"added": {"movies": movies, "episodes": eps}, "not_found": {}}


def remove_ok(body, _params):
    return {"deleted": {"episodes": len(body.get("ids") or [])}, "not_found": {"ids": []}}


def bitfield(n_videos: int, watched_idx, anchor_id: str) -> str:
    """Build a Stremio `state.watched` string the way stremio-core serialises it."""
    buf = bytearray((n_videos + 7) // 8)
    for i in watched_idx:
        buf[i // 8] |= 1 << (i % 8)
    last = max(watched_idx)
    payload = base64.b64encode(zlib.compress(bytes(buf))).decode()
    return f"{anchor_id}:{last + 1}:{payload}"


def network_error(path="x"):
    return track.TraktError(f"Síťová chyba u {path}: timed out")


def write_json(path, data):
    path.write_text(json.dumps(data))
