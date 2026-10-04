#!/usr/bin/env python3
"""Diagnostika: projde celý řetěz a řekne, co je rozbité a jak to opravit.

  python3 doctor.py              # všechno, včetně síťových kontrol
  python3 doctor.py --offline    # jen lokální stav (bez Traktu a Stremia)
  python3 doctor.py --verify-refresh   # navíc vynutí skutečnou obnovu Trakt tokenu
  python3 doctor.py --json

Exit code: 0 vše v pořádku, 10 jen varování, 1 něco je rozbité.

Obnova tokenu se ověřuje voláním, ale ne pokaždé: refresh token je jednorázový
(Trakt ho při obnově vymění), takže každá zbytečná obnova je malé riziko — kdyby
spojení spadlo po výměně, starý token už neplatí. Doctor proto obnovu skutečně
provede, jen když poslední úspěšná obnova není zaznamenaná v auth_state.json
z doby kratší než `REFRESH_PROOF_DAYS`, když token brzy vyprší, nebo s
--verify-refresh. Jinak je důkazem ta zaznamenaná úspěšná obnova.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import pathlib
import sqlite3
import sys

CODE_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(CODE_DIR))

import common  # noqa: E402

HERE = common.DATA_DIR
REFRESH_PROOF_DAYS = 8
OK, WARN, FAIL, SKIP = "ok", "warn", "FAIL", "skip"
ICON = {OK: "✅", WARN: "⚠️ ", FAIL: "❌", SKIP: "·"}


def age_hours(epoch: float) -> float:
    return (common.now() - epoch) / 3600


def fmt_age(hours: float) -> str:
    return f"{hours:.0f} h" if hours < 48 else f"{hours / 24:.1f} d"


def check(name, status, detail, fix=""):
    return {"name": name, "status": status, "detail": detail, "fix": fix}


# ------------------------------------------------------------------ síť


def check_trakt(verify_refresh: bool) -> list[dict]:
    import track
    out = []
    cfg_path = HERE / "config.json"
    if not cfg_path.exists():
        return [check("Trakt přihlášení", FAIL, "chybí config.json", "bash setup_secret.sh && python3 track.py auth")]
    remaining = track.token_remaining()
    if remaining is None:
        return [check("Trakt přihlášení", FAIL, "v config.json není token", "python3 track.py auth")]
    state = common.read_json(HERE / "auth_state.json", {})
    proof = state.get("refresh_ok_epoch")
    must_refresh = verify_refresh or remaining < track.REFRESH_MARGIN or \
        not proof or age_hours(float(proof)) > REFRESH_PROOF_DAYS * 24
    if must_refresh:
        try:
            track.refresh_token()
            out.append(check("Trakt obnova tokenu", OK, "ověřeno voláním: token obnoven"))
            remaining = track.token_remaining()
        except track.TraktError as e:
            track._note_refresh(False, str(e))
            status = FAIL if remaining < 2 * 86400 else WARN
            out.append(check("Trakt obnova tokenu", status,
                             f"selhala ({e}); token platí ještě {fmt_age(max(remaining, 0) / 3600)}",
                             "python3 track.py auth"))
    else:
        out.append(check("Trakt obnova tokenu", OK,
                         f"naposledy úspěšně před {fmt_age(age_hours(float(proof)))} "
                         "(skutečné ověření: --verify-refresh)"))
    if state.get("refresh_failed_at") and must_refresh is False:
        out.append(check("Trakt obnova tokenu", WARN,
                         f"poslední pokus selhal {state['refresh_failed_at']}: {state.get('refresh_error')}",
                         "python3 track.py auth"))
    if remaining is not None and remaining <= 0:
        out.append(check("Trakt token", FAIL, "vypršel", "python3 track.py auth"))
        return out
    try:
        payload, headers = track._req("GET", "/users/settings")
        user = ((payload or {}).get("user") or {}).get("username") or "?"
        detail = f"účet {user}, token platí ještě {fmt_age(remaining / 3600)}"
        limit = headers.get("x-ratelimit")
        if limit:
            try:
                rl = json.loads(limit)
                detail += f", limit {rl.get('remaining')}/{rl.get('limit')} za {rl.get('period')} s"
            except ValueError:
                pass
        out.append(check("Trakt API", OK, detail))
    except SystemExit as e:
        out.append(check("Trakt API", FAIL, f"přihlášení nefunguje (rc {e.code})", "python3 track.py auth"))
    except track.TraktError as e:
        if "401" in str(e):
            out.append(check("Trakt API", FAIL, str(e), "python3 track.py auth"))
        else:
            out.append(check("Trakt API", WARN, f"nedostupné: {e}",
                             "počkej; cron to zkusí znovu (krátký výpadek Traktu)"))
    return out


def check_stremio() -> dict:
    import urllib.request
    import stremio_bridge as b
    if not b.STREMIO_CFG.exists():
        return check("Stremio přihlášení", FAIL, "chybí stremio.json", "bash setup_stremio.sh")
    cfg = json.loads(b.STREMIO_CFG.read_text())
    req = urllib.request.Request(
        b.SAPI + "/datastoreMeta",
        data=json.dumps({"authKey": cfg.get("authKey"), "collection": "libraryItem"}).encode(),
        headers={"Content-Type": "application/json", "User-Agent": b.UA}, method="POST")
    try:
        data = b._http_json(req, 30, "Stremio API")
    except b.BridgeError as e:
        return check("Stremio API", WARN, f"nedostupné: {e}", "počkej; cron to zkusí znovu")
    err = data.get("error") if isinstance(data, dict) else None
    if err:
        msg = err.get("message") if isinstance(err, dict) else str(err)
        return check("Stremio přihlášení", FAIL, f"neplatí ({msg}) — most nic nedoplňuje",
                     "bash setup_stremio.sh")
    return check("Stremio API", OK, "přihlášení platí")


# ---------------------------------------------------------------- lokálně


def check_watchdog() -> dict:
    data = common.read_json(common.LAST_SUCCESS, None)
    if not isinstance(data, dict) or not data.get("epoch"):
        return check("Denní běh", WARN, "zatím žádný úspěšný běh (last_success.json chybí)",
                     "python3 cron_daily.py ručně, pak ověř cron/launchd (README: When tracking stops)")
    h = age_hours(float(data["epoch"]))
    stale = common.stale_message()
    if stale:
        return check("Denní běh", FAIL, stale,
                     "zjisti, proč neběží: crontab -l / launchctl list | grep stremio; ruční běh: python3 cron_daily.py")
    return check("Denní běh", OK, f"poslední úspěch před {fmt_age(h)} ({', '.join(data.get('steps') or [])})")


def check_library() -> list[dict]:
    import stremio_bridge as b
    out = []
    if not b.RAW.exists():
        return [check("Knihovna Stremia", WARN, "stremio_library.json chybí", "python3 stremio_bridge.py fetch")]
    items = common.read_json(b.RAW, [])
    h = age_hours(b.RAW.stat().st_mtime)
    series = [i for i in items if i.get("type") == "series" and (i.get("state") or {}).get("watched")]
    bad = []
    for i in series:
        try:
            b.decode_watched(i["state"]["watched"])
        except ValueError as e:
            bad.append(f"{i.get('name')}: {e}")
    status = WARN if bad or h > 36 else OK
    out.append(check("Knihovna Stremia", status,
                     f"{len(items)} položek, {len(series)} seriálů s bitovou mapou, nečitelných {len(bad)}, "
                     f"staženo před {fmt_age(h)}" + (f"; {bad[0]}" if bad else ""),
                     "python3 stremio_bridge.py fetch" if h > 36 else ""))
    gaps = common.read_json(b.GAPS, None)
    if gaps is None:
        out.append(check("Porovnání (gaps.json)", SKIP, "ještě neproběhlo", "python3 stremio_bridge.py compare"))
    else:
        unknown = gaps.get("unknown") or []
        missing = sum(len(s.get("missing") or []) for s in gaps.get("shows") or []) + len(gaps.get("movies") or [])
        detail = (f"chybí {missing} položek, neověřeno {len(unknown)}"
                  + "".join(f"; {u.get('name')}: {u.get('reason')}" for u in unknown[:5]))
        if gaps.get("beyond_anchor"):
            detail += f"; díly za kotvou u {len(gaps['beyond_anchor'])} seriálů"
        out.append(check("Porovnání (gaps.json)", WARN if unknown else OK, detail,
                         "python3 stremio_bridge.py forward (dry-run), případně exact --only <imdb>"
                         if unknown else ""))
    return out


def check_caches() -> dict:
    ttl = float(common.settings()["cache_ttl_hours"]) * 3600
    parts = []
    for name in ("cinemeta_cache.json", "trakt_episodes_cache.json"):
        data = common.read_json(HERE / name, {})
        entries = [v for v in data.values() if isinstance(v, dict) and "t" in v] if isinstance(data, dict) else []
        stale = sum(1 for v in entries if common.now() - float(v["t"]) >= ttl)
        parts.append(f"{name}: {len(entries)} položek, po TTL {stale}")
    return check("Cache", OK, "; ".join(parts) + " (po TTL se obnoví samo)")


def check_bridge_state() -> list[dict]:
    out = []
    for name in ("last_exact.json", "last_redate.json", "last_restore.json"):
        data = common.read_json(HERE / name, None)
        if not isinstance(data, dict):
            continue
        statuses = [data.get("status")] + [r.get("status") for r in data.get("shows") or []]
        bad = [s for s in statuses if s and s != "done"]
        if bad:
            out.append(check(f"Deník {name}", WARN, f"stav: {', '.join(bad)}",
                             f"prohlédni {name}; vrátit: python3 stremio_bridge.py restore --from-journal {name}"))
    repeats = []
    for name in ("last_push.json", "last_forward.json"):
        repeats += (common.read_json(HERE / name, {}) or {}).get("repeat") or []
    if repeats:
        out.append(check("Opakované zápisy", WARN, f"{len(repeats)} zastaveno pojistkou",
                         "prověř, proč je Trakt nevidí (skrytá řada, sloučené ID)"))
    log = HERE / "write_log.jsonl"
    n = sum(1 for _ in log.open()) if log.exists() else 0
    out.append(check("write_log.jsonl", OK, f"{n} záznamů"))
    lock = HERE / ".lock"
    if lock.exists():
        fd = os.open(lock, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            out.append(check("Zámek", OK, "volný"))
        except BlockingIOError:
            out.append(check("Zámek", WARN, "drží ho běžící proces (právě běží zápis, nebo visí)",
                             "ps aux | grep -E 'cron_daily|stremio_bridge'"))
        finally:
            os.close(fd)
    return out


def check_backup() -> list[dict]:
    """Lokální snímek (primární) a kopie mimo stroj (bonus). Cíl mimo stroj se
    nečte přímo — jen odděleným procesem s časovým limitem, protože mrtvá
    cloudová složka umí zablokovat i výpis adresáře."""
    import backup_db
    out = []
    local = backup_db.local_dir()
    newest, age = backup_db.newest_local(local)
    if newest is None:
        out.append(check("Záloha lokálně", FAIL, f"v {local} žádný snímek — databáze nemá zálohu",
                         "python3 backup_db.py; ověř job zálohy v cronu/launchd"))
    else:
        status = OK if age <= 48 else FAIL
        out.append(check("Záloha lokálně", status, f"nejnovější {newest.name} před {fmt_age(age)}",
                         "" if status == OK else "python3 backup_db.py; ověř job zálohy v cronu/launchd"))
    dst = backup_db.offsite_dir()
    if dst is None:
        out.append(check("Záloha mimo stroj", SKIP, "vypnuto (backup_dir = off)"))
        return out
    meta = common.read_json(local / "last_backup.json", {})
    info, err = backup_db.probe_offsite(dst)
    fix = ("lokální snímky jsou v pořádku; zkontroluj cíl (iCloud: přihlášení a iCloud Drive "
           "v Nastavení), nebo nastav settings.backup_dir na jinou složku či \"off\"")
    if info is None:
        last_ok = meta.get("offsite_ok_at") or "nikdy"
        out.append(check("Záloha mimo stroj", WARN,
                         f"mimo stroj se nezálohuje (cíl nedostupný: {err}); poslední úspěch: {last_ok}",
                         fix))
    elif not info["count"] or (info["age_h"] or 0) > 48:
        reason = meta.get("offsite_reason")
        if not info.get("exists", True):
            what = "cílová složka neexistuje — mimo stroj se zatím nic nezazálohovalo"
        elif info["age_h"] is None:
            what = "ve složce cíle není žádný snímek"
        else:
            what = f"nejnovější snímek mimo stroj je před {fmt_age(info['age_h'])}"
        out.append(check("Záloha mimo stroj", WARN,
                         what + (f"; poslední pokus: {reason}" if reason and reason != "ok" else ""), fix))
    else:
        out.append(check("Záloha mimo stroj", OK,
                         f"{info['count']} snímků, nejnovější před {fmt_age(info['age_h'])}"))
    return out


def check_db() -> dict:
    db = HERE / "tracker.db"
    if not db.exists():
        return check("Databáze", WARN, "tracker.db chybí", "python3 track.py sync")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        cols = {r[1] for r in con.execute("PRAGMA table_info(watched_episodes)")}
        live = " WHERE deleted_at IS NULL" if "deleted_at" in cols else ""
        mv = con.execute(f"SELECT COUNT(*) FROM watched_movies{live}").fetchone()[0]
        ep = con.execute(f"SELECT COUNT(*), MIN(watched_at) FILTER (WHERE watched_at >= '2000'), "
                         f"MAX(watched_at) FROM watched_episodes{live}").fetchone()
        synced = con.execute("SELECT value FROM meta WHERE key='synced_at'").fetchone()
    except sqlite3.Error as e:
        return check("Databáze", FAIL, f"chyba: {e}", "obnov ze zálohy (backup_db.py --list)")
    finally:
        con.close()
    if integrity != "ok":
        return check("Databáze", FAIL, f"integrity_check: {integrity}", "obnov ze zálohy (backup_db.py --list)")
    detail = f"integrita ok, {mv} filmů, {ep[0]} epizod, {str(ep[1])[:10]} – {str(ep[2])[:10]}"
    status = OK
    if synced:
        try:
            h = age_hours(dt.datetime.fromisoformat(synced[0]).timestamp())
            detail += f", sync před {fmt_age(h)}"
            status = OK if h <= 36 else WARN
        except ValueError:
            pass
    return check("Databáze", status, detail, "" if status == OK else "python3 track.py sync")


def run_checks(offline: bool = False, verify_refresh: bool = False) -> list[dict]:
    results = [check_watchdog()]
    if offline:
        results.append(check("Trakt / Stremio", SKIP, "--offline"))
    else:
        for label, fn, fix in (
                ("Trakt přihlášení", lambda: check_trakt(verify_refresh),
                 "zkontroluj config.json, případně bash setup_secret.sh && python3 track.py auth"),
                ("Stremio přihlášení", check_stremio, "bash setup_stremio.sh")):
            try:
                r = fn()
            except SystemExit as e:
                r = check(label, FAIL, f"skončilo s rc {e.code}", fix)
            except Exception as e:  # noqa: BLE001 — poškozený soubor je přesně to, co má doctor ukázat
                r = check(label, FAIL, f"{type(e).__name__}: {e}", fix)
            results += r if isinstance(r, list) else [r]
    for fn in (check_library, check_caches, check_bridge_state, check_backup, check_db):
        try:
            r = fn()
        except Exception as e:  # noqa: BLE001 — doctor nesmí spadnout na jedné kontrole
            r = check(fn.__name__.removeprefix("check_"), FAIL, f"{type(e).__name__}: {e}")
        results += r if isinstance(r, list) else [r]
    return results


def exit_code(results: list[dict]) -> int:
    statuses = {r["status"] for r in results}
    if FAIL in statuses:
        return common.EXIT_ERROR
    if WARN in statuses:
        return common.EXIT_WARN
    return common.EXIT_OK


def main() -> None:
    ap = argparse.ArgumentParser(description="Diagnostika trackeru")
    ap.add_argument("--offline", action="store_true", help="bez síťových kontrol")
    ap.add_argument("--verify-refresh", action="store_true",
                    help="vynutit skutečnou obnovu Trakt tokenu")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    results = run_checks(args.offline, args.verify_refresh)
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=1))
    else:
        for r in results:
            print(f"{ICON[r['status']]} {r['name']}: {r['detail']}")
            if r["fix"] and r["status"] in (WARN, FAIL):
                print(f"     → {r['fix']}")
    sys.exit(exit_code(results))


if __name__ == "__main__":
    main()
