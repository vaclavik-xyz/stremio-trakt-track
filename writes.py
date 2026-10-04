"""The only module that writes to Trakt history.

Every add and remove goes through here, so that all commands share:
- an append-only `write_log.jsonl` (what was written or removed, when, with the
  original `watched_at` and history id — enough to undo it),
- the repeat guard: Trakt counts every write as one more play and never
  deduplicates, so an item added again within `repeat_guard_days` (with no
  removal in between) is skipped and reported as an anomaly instead,
- the rate limit pause between writes.

Items are plain dicts:
  {"kind": "movie",   "ids": {"imdb": "tt…"}, "watched_at": "…"|None, "name": "…"}
  {"kind": "episode", "ids": {"imdb": "tt…"}, "season": 1, "episode": 2, …}
For episodes `ids` are the show's ids.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import time

import common

WRITE_LOG = common.DATA_DIR / "write_log.jsonl"
WRITE_SLEEP = 1.2           # Trakt allows ~1 write per second
BATCH = 100


def _track():
    import track
    return track


def movie_item(ids: dict, watched_at: str | None = None, name: str | None = None) -> dict:
    return {"kind": "movie", "ids": dict(ids), "watched_at": watched_at, "name": name}


def episode_item(ids: dict, season: int, episode: int, watched_at: str | None = None,
                 name: str | None = None) -> dict:
    return {"kind": "episode", "ids": dict(ids), "season": int(season), "episode": int(episode),
            "watched_at": watched_at, "name": name}


def _id_key(ids: dict) -> str:
    for k in ("imdb", "trakt", "tmdb", "tvdb"):
        if ids.get(k):
            return f"{k}:{ids[k]}"
    return json.dumps(ids, sort_keys=True)


def item_key(item: dict) -> str:
    if item["kind"] == "movie":
        return f"movie|{_id_key(item['ids'])}"
    return f"episode|{_id_key(item['ids'])}|{item['season']}|{item['episode']}"


# ------------------------------------------------------------------- log


def log_records() -> list[dict]:
    if not WRITE_LOG.exists():
        return []
    out = []
    for line in WRITE_LOG.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def append_log(records: list[dict]) -> None:
    if not records:
        return
    fd = os.open(WRITE_LOG, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        data = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
        os.write(fd, data.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def _record(op: str, cmd: str, item: dict, **extra) -> dict:
    now = time.time()
    rec = {"ts": dt.datetime.fromtimestamp(now).astimezone().isoformat(timespec="seconds"),
           "epoch": now, "op": op, "cmd": cmd, "key": item_key(item),
           **{k: v for k, v in item.items() if v is not None}}
    rec.update(extra)
    return rec


def recent_adds(days: float) -> dict[str, float]:
    """Keys added in the last `days` days and not removed since."""
    state: dict[str, float] = {}
    for rec in log_records():
        key = rec.get("key")
        if not key:
            continue
        if rec.get("op") == "add":
            state[key] = float(rec.get("epoch") or 0)
        elif rec.get("op") == "remove":
            state.pop(key, None)
    cutoff = time.time() - days * 86400
    return {k: t for k, t in state.items() if t >= cutoff}


# ------------------------------------------------------------------ add


def _payload(batch: list[dict]) -> dict:
    if batch[0]["kind"] == "movie":
        return {"movies": [{"ids": i["ids"], "watched_at": i.get("watched_at") or "unknown"}
                           for i in batch]}
    seasons: dict[int, list] = {}
    for i in batch:
        seasons.setdefault(i["season"], []).append(
            {"number": i["episode"], "watched_at": i.get("watched_at") or "unknown"})
    return {"shows": [{"ids": batch[0]["ids"],
                       "seasons": [{"number": n, "episodes": eps}
                                   for n, eps in sorted(seasons.items())]}]}


def _batches(items: list[dict]) -> list[list[dict]]:
    movies = [i for i in items if i["kind"] == "movie"]
    out = [movies[k:k + BATCH] for k in range(0, len(movies), BATCH)]
    by_show: dict[str, list[dict]] = {}
    for i in items:
        if i["kind"] == "episode":
            by_show.setdefault(_id_key(i["ids"]), []).append(i)
    out += list(by_show.values())
    return out


def add_history(items: list[dict], *, cmd: str, force_repeat: bool = False,
                guard_days: float | None = None, on_progress=None) -> dict:
    """Add items to Trakt history. Returns {"added", "not_found", "uncertain",
    "repeat", "denied"}; `on_progress(result)` runs after every request so the
    caller can persist its journal. Network errors propagate."""
    if guard_days is None:
        guard_days = common.settings()["repeat_guard_days"]
    result: dict = {"added": [], "not_found": [], "uncertain": [], "repeat": [], "denied": []}
    recent = {} if force_repeat else recent_adds(guard_days)
    todo = []
    seen = set()
    for i in items:
        key = item_key(i)
        if key in seen:
            continue                     # never send the same item twice in one run
        seen.add(key)
        if key in recent:
            result["repeat"].append(i)
        else:
            todo.append(i)
    if result["repeat"] and on_progress:
        on_progress(result)

    track = _track()
    for batch in _batches(todo):
        res = track._req("POST", "/sync/history", body=_payload(batch))[0] or {}
        kind = "movies" if batch[0]["kind"] == "movie" else "episodes"
        added = int((res.get("added") or {}).get(kind) or 0)
        nf = res.get("not_found") or {}
        if kind == "movies":
            missing = {_id_key(x.get("ids") or {}) for x in nf.get("movies") or []}
        else:
            missing = {_id_key(x.get("ids") or {}) for x in nf.get("shows") or []}
        not_found = [i for i in batch if _id_key(i["ids"]) in missing]
        rest = [i for i in batch if _id_key(i["ids"]) not in missing]
        result["not_found"] += not_found
        if added == len(rest):
            result["added"] += rest
            append_log([_record("add", cmd, i) for i in rest])
        else:
            # Trakt took some but does not say which. Log them anyway so the repeat
            # guard errs on the side of no duplicates, and let a human look.
            result["uncertain"] += rest
            label = (rest[0].get("name") or _id_key(rest[0]["ids"])) if rest else "?"
            result["denied"].append(f"{label}: Trakt accepted {added} of {len(rest)}")
            append_log([_record("add", cmd, i, confirmed=False) for i in rest])
        for i in not_found:
            result["denied"].append(f"{i.get('name') or _id_key(i['ids'])}: not found on Trakt")
        if on_progress:
            on_progress(result)
        time.sleep(WRITE_SLEEP)
    return result


# --------------------------------------------------------------- remove


def fetch_show_history(show_id: str) -> list[dict]:
    """Every history entry (one per play) of a show, with its history id."""
    rows = _track().paged(f"/sync/history/shows/{show_id}")
    out = []
    for r in rows:
        ep = r.get("episode") or {}
        if r.get("type") != "episode" or not r.get("id"):
            continue
        ids = {k: v for k, v in ((r.get("show") or {}).get("ids") or {}).items()
               if k in ("imdb", "trakt", "tmdb", "tvdb") and v}
        out.append({"history_id": r["id"], "kind": "episode", "ids": ids,
                    "season": ep.get("season"), "episode": ep.get("number"),
                    "watched_at": r.get("watched_at"),
                    "name": (r.get("show") or {}).get("title")})
    return out


def remove_history(entries: list[dict], *, cmd: str, on_progress=None) -> dict:
    """Remove exact history entries (by history id). Each removal is logged with its
    original `watched_at`, so `restore` can put it back."""
    result: dict = {"removed": [], "not_found": []}
    track = _track()
    entries = [e for e in entries if e.get("history_id")]
    for k in range(0, len(entries), BATCH):
        batch = entries[k:k + BATCH]
        res = track._req("POST", "/sync/history/remove",
                         body={"ids": [e["history_id"] for e in batch]})[0] or {}
        missing = set((res.get("not_found") or {}).get("ids") or [])
        gone = [e for e in batch if e["history_id"] not in missing]
        result["removed"] += gone
        result["not_found"] += [e for e in batch if e["history_id"] in missing]
        append_log([_record("remove", cmd, e) for e in gone])
        if on_progress:
            on_progress(result)
        time.sleep(WRITE_SLEEP)
    return result
