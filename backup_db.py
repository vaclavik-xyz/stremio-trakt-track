#!/usr/bin/env python3
"""Záloha tracker.db — vždy lokální snímek, kopie mimo stroj jen jako bonus.

1. **Lokální snímek** (`backup_local_dir`, výchozí `<data>/backups/`) je primární
   a musí vzniknout vždycky: konzistentní kopie přes `VACUUM INTO`, ověřená
   `PRAGMA integrity_check` a počty řádků. Když selže, je to chyba (rc ≠ 0).
2. **Kopie mimo stroj** (`backup_dir`, výchozí iCloud Drive; "off" = vypnuto) je
   best-effort s tvrdým časovým stropem `backup_offsite_timeout_s`. Běží
   v odděleném procesu: cloudová složka umí zamrznout tak, že visí i obyčejný
   výpis adresáře (mrtvý `bird` u iCloudu) a vlákno zaseknuté v systémovém volání
   nejde ukončit — proces jde zabít a opustit. Po vypršení se jen varuje.

Živou SQLite databázi nedávej rovnou do synchronizované složky: synchronizace na
úrovni souborů umí databázi rozbít. Proto se kopíruje jen hotový ověřený snímek.

Použití:
    python3 backup_db.py              # lokální snímek + kopie mimo stroj + úklid
    python3 backup_db.py --list       # co je v zálohách (mimo stroj s časovým limitem)
    python3 backup_db.py --to DIR     # jiný cíl mimo stroj (nebo TRAKT_BACKUP_DIR)
    python3 backup_db.py --no-offsite # jen lokálně

Exit code: 0 lokální snímek hotový (stav kopie mimo stroj je v last_backup.json
a ve varování), 1 lokální snímek selhal.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import shutil
import sqlite3
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import common  # noqa: E402

HERE = common.DATA_DIR
DB = HERE / "tracker.db"
TABLES = ("watched_movies", "watched_episodes", "shows_progress")
ICLOUD = (pathlib.Path.home() / "Library/Mobile Documents/com~apple~CloudDocs"
          / "trakt-tracker-backups")
COPY_ALSO = ["alias.json"]          # co je malé a nenahraditelné
KEEP_DAYS = 30                      # denní snapshots za posledních 30 dní
KEEP_MONTHS = 12                    # + poslední snapshot z každého měsíce
PROBE_TIMEOUT = 10                  # doctor / --list: jak dlouho čekat na cíl mimo stroj
OFF = {"", "off", "none", "false", "0"}


def local_dir(arg: str | None = None) -> pathlib.Path:
    raw = arg or common.settings().get("backup_local_dir")
    return pathlib.Path(raw).expanduser() if raw else HERE / "backups"


def offsite_dir(arg: str | None = None) -> pathlib.Path | None:
    """Cíl mimo stroj: --to, TRAKT_BACKUP_DIR, settings.backup_dir, jinak iCloud.
    Hodnota "off" (nebo false / prázdná) ho vypne."""
    raw = arg if arg is not None else os.environ.get("TRAKT_BACKUP_DIR")
    if raw is None:
        raw = common.settings().get("backup_dir")
        if raw is None:
            return ICLOUD
    if raw is False or str(raw).strip().lower() in OFF:
        return None
    return pathlib.Path(str(raw)).expanduser()


def offsite_timeout() -> float:
    try:
        return float(common.settings()["backup_offsite_timeout_s"])
    except (KeyError, TypeError, ValueError):
        return float(common.DEFAULTS["backup_offsite_timeout_s"])


def snapshots(dst: pathlib.Path) -> list[pathlib.Path]:
    return sorted(dst.glob("tracker-*.sqlite"))


def make_snapshot(dst: pathlib.Path) -> pathlib.Path:
    """Konzistentní kopie databáze. `VACUUM INTO` zvládne i otevřený originál."""
    if not DB.exists():
        sys.exit(f"Chybí {DB} — není co zálohovat.")
    dst.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y-%m-%d")
    out = dst / f"tracker-{stamp}.sqlite"
    if out.exists():
        out = dst / f"tracker-{stamp}-{dt.datetime.now().strftime('%H%M%S')}.sqlite"
    tmp = out.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    src = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        before = _counts(src)
        src.execute("VACUUM INTO ?", (str(tmp),))
        after = _counts(src)
    finally:
        src.close()
    os.chmod(tmp, 0o600)            # osobní data: hned, ne až po kontrole

    # kontrola: kopie se musí dát otevřít a mít stejně záznamů jako originál.
    # Souběžný `sync` může mezitím commitnout, proto stačí shoda se stavem těsně
    # před nebo těsně po snímku.
    con = sqlite3.connect(tmp)
    try:
        check = con.execute("PRAGMA integrity_check").fetchone()[0]
        got = _counts(con)
    finally:
        con.close()
    if check != "ok":
        tmp.unlink(missing_ok=True)
        sys.exit(f"Snímek neprošel kontrolou integrity ({check}) — zahozen.")
    if got not in (before, after):
        tmp.unlink(missing_ok=True)
        sys.exit(f"Snímek nesedí ({got} vs {before}/{after}) — zahozen.")
    tmp.replace(out)

    for name in COPY_ALSO:
        src_file = HERE / name
        if src_file.exists():
            shutil.copy2(src_file, dst / name)
    return out


def _counts(con: sqlite3.Connection) -> tuple:
    return tuple(con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES)


def snapshot_date(path: pathlib.Path) -> dt.date:
    """Datum z názvu `tracker-YYYY-MM-DD[...]`; mtime jen jako záloha — iCloud ho
    při stažení nebo obnově umí změnit."""
    m = re.match(r"tracker-(\d{4}-\d{2}-\d{2})", path.name)
    if m:
        try:
            return dt.date.fromisoformat(m.group(1))
        except ValueError:
            pass
    return dt.date.fromtimestamp(path.stat().st_mtime)


def prune(dst: pathlib.Path, today: dt.date | None = None) -> list[pathlib.Path]:
    """Nechá všechny snapshots za posledních 30 dní + nejnovější z posledních 12 měsíců."""
    snaps = sorted(snapshots(dst), key=lambda s: (snapshot_date(s), s.name))
    if not snaps:
        return []
    cut = (today or dt.date.today()) - dt.timedelta(days=KEEP_DAYS)
    keep = {s for s in snaps if snapshot_date(s) >= cut}
    monthly: dict[str, pathlib.Path] = {}
    for s in snaps:
        monthly[snapshot_date(s).strftime("%Y-%m")] = s   # seřazené → nejnovější z měsíce
    keep |= set(list(monthly.values())[-KEEP_MONTHS:])
    keep.add(snaps[-1])
    removed = []
    for s in snaps:
        if s not in keep:
            s.unlink()
            removed.append(s)
    return removed


def newest_local(dst: pathlib.Path) -> tuple[pathlib.Path | None, float | None]:
    """Nejnovější lokální snímek a jeho stáří v hodinách."""
    snaps = snapshots(dst) if dst.exists() else []
    if not snaps:
        return None, None
    newest = max(snaps, key=lambda s: s.stat().st_mtime)
    return newest, (common.now() - newest.stat().st_mtime) / 3600


# ------------------------------------------------- mimo stroj, s časovým limitem
# Všechno, co sahá na cílovou složku mimo stroj, běží v odděleném procesu
# (skryté přepínače --_offsite-copy / --_offsite-probe níže).


def run_guarded(argv: list[str], timeout: float) -> tuple[bool, str]:
    """Spustí proces s tvrdým limitem. Po vypršení ho zabije a nečeká na něj déle
    než 2 s — proces zaseknutý v jádře (mrtvý síťový/cloudový FS) se nemusí dát
    zabít hned, ale nás to blokovat nesmí. Std vstupy/výstupy jsou vlastní, takže
    opuštěný proces nedrží výstup cronu."""
    p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, start_new_session=True)
    try:
        out, _ = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        try:
            p.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            if p.stdout:
                p.stdout.close()
        return False, f"cíl neodpověděl do {timeout:.0f} s (nedostupný?)"
    out = (out or "").strip()
    if p.returncode != 0:
        return False, out.splitlines()[-1] if out else f"rc {p.returncode}"
    return True, out


def _self(*args: str) -> list[str]:
    return [sys.executable, str(pathlib.Path(__file__).resolve()), *args]


def offsite_copy(snapshot: pathlib.Path, dst: pathlib.Path) -> None:
    """(v odděleném procesu) zkopíruje hotový snímek do cíle mimo stroj."""
    dst.mkdir(parents=True, exist_ok=True)
    for stale in dst.glob(".tracker-*.partial"):     # zbytek po přerušené kopii
        stale.unlink()
    tmp = dst / f".{snapshot.name}.partial"
    shutil.copy2(snapshot, tmp)
    os.chmod(tmp, 0o600)
    os.replace(tmp, dst / snapshot.name)
    for name in COPY_ALSO:
        src_file = HERE / name
        if src_file.exists():
            shutil.copy2(src_file, dst / name)
    prune(dst)


def offsite_probe(dst: pathlib.Path) -> dict:
    """(v odděleném procesu) stav cíle mimo stroj."""
    if not dst.exists():
        return {"exists": False, "count": 0, "newest": None, "age_h": None}
    snaps = snapshots(dst)
    if not snaps:
        return {"exists": True, "count": 0, "newest": None, "age_h": None}
    newest = max(snaps, key=lambda s: s.stat().st_mtime)
    return {"exists": True, "count": len(snaps), "newest": newest.name,
            "age_h": (common.now() - newest.stat().st_mtime) / 3600,
            "size_kb": sum(s.stat().st_size for s in snaps) / 1024}


def worker_main(argv: list[str]) -> int:
    """Vstup odděleného procesu. Chyba = jeden řádek na výstupu a rc 1, nikdy traceback."""
    try:
        if argv[0] == "--_offsite-copy":
            offsite_copy(pathlib.Path(argv[1]), pathlib.Path(argv[2]))
            print("ok")
        else:
            print(json.dumps(offsite_probe(pathlib.Path(argv[1]))))
        return 0
    except Exception as e:  # noqa: BLE001 — InterruptedError, PermissionError, …
        print(f"cíl nedostupný: {type(e).__name__}: {e}")
        return 1


def copy_offsite(snapshot: pathlib.Path, dst: pathlib.Path,
                 timeout: float | None = None) -> tuple[bool, str]:
    return run_guarded(_self("--_offsite-copy", str(snapshot), str(dst)),
                       offsite_timeout() if timeout is None else timeout)


def probe_offsite(dst: pathlib.Path, timeout: float = PROBE_TIMEOUT) -> tuple[dict | None, str]:
    ok, out = run_guarded(_self("--_offsite-probe", str(dst)), timeout)
    if not ok:
        return None, out
    try:
        return json.loads(out.splitlines()[-1]), ""
    except (ValueError, IndexError):
        return None, f"nečitelná odpověď: {out[:80]}"


# ------------------------------------------------------------------- main


def backup(local: pathlib.Path, offsite: pathlib.Path | None) -> dict:
    """Lokální snímek (chyba = sys.exit) + kopie mimo stroj (jen varování)."""
    out = make_snapshot(local)
    removed = prune(local)
    prev = common.read_json(local / "last_backup.json", {})
    meta = {"created": dt.datetime.now().isoformat(timespec="seconds"),
            "local": str(out), "offsite_dir": str(offsite) if offsite else None,
            "removed": [s.name for s in removed]}
    con = sqlite3.connect(out)
    try:
        meta["db_records"] = sum(con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                                 for t in TABLES[:2])
    finally:
        con.close()
    if offsite is None:
        meta.update(offsite=False, offsite_reason="vypnuto (backup_dir = off)")
    else:
        ok, msg = copy_offsite(out, offsite)
        meta.update(offsite=ok, offsite_reason="ok" if ok else msg)
    if meta["offsite"]:
        meta["offsite_ok_at"] = meta["created"]
    elif prev.get("offsite_ok_at"):
        meta["offsite_ok_at"] = prev["offsite_ok_at"]
    meta["offsite_warned_epoch"] = prev.get("offsite_warned_epoch")
    common.atomic_write_json(local / "last_backup.json", meta)
    return meta


def offsite_warning_due(meta: dict, prev_reason: str | None) -> bool:
    """Neopakovat stejné varování každou noc: při změně a pak po issue_remind_days."""
    if meta["offsite"] or meta["offsite_dir"] is None:
        return False
    last = meta.get("offsite_warned_epoch")
    remind = max(1, int(common.settings()["issue_remind_days"])) * 86400
    return prev_reason != meta["offsite_reason"] or not last or common.now() - float(last) >= remind


def main() -> None:
    if len(sys.argv) >= 3 and sys.argv[1] in ("--_offsite-copy", "--_offsite-probe"):
        sys.exit(worker_main(sys.argv[1:]))
    ap = argparse.ArgumentParser(description="Záloha tracker.db (lokálně + mimo stroj)")
    ap.add_argument("--to", help="cíl mimo stroj (výchozí iCloud; 'off' vypne)")
    ap.add_argument("--local-dir", help="lokální složka (výchozí <data>/backups)")
    ap.add_argument("--no-offsite", action="store_true", help="jen lokální snímek")
    ap.add_argument("--list", action="store_true", help="jen vypsat, co je v zálohách")
    ap.add_argument("--quiet", action="store_true",
                    help="nic nevypisovat, když záloha projde (pro cron)")
    args = ap.parse_args()
    local = local_dir(args.local_dir)
    offsite = None if args.no_offsite else offsite_dir(args.to)

    if args.list:
        snaps = snapshots(local) if local.exists() else []
        print(f"Lokálně {local} — {len(snaps)} snímků")
        for s in snaps:
            when = dt.datetime.fromtimestamp(s.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            print(f"  {s.name:36s} {when}  {s.stat().st_size / 1024:.0f} kB")
        if offsite:
            info, err = probe_offsite(offsite)
            if info is None:
                print(f"Mimo stroj {offsite}: {err}")
            else:
                print(f"Mimo stroj {offsite} — {info['count']} snímků, nejnovější {info['newest']}")
        return

    prev_reason = common.read_json(local / "last_backup.json", {}).get("offsite_reason")
    meta = backup(local, offsite)
    warn = offsite_warning_due(meta, prev_reason)
    if warn:
        meta["offsite_warned_epoch"] = common.now()
        common.atomic_write_json(local / "last_backup.json", meta)
    # nezávislý hlídač denního běhu: záloha běží jako samostatný job, takže se
    # ozve, i když denní doplnění přestalo běžet (i s --quiet)
    stale = common.stale_message()
    if stale:
        print(stale)
    if warn or (not args.quiet and not meta["offsite"] and meta["offsite_dir"]):
        print(f"⚠ Mimo stroj se nezálohuje ({meta['offsite_reason']}). Lokální snímek je "
              f"v pořádku: {meta['local']}. Cíl: {meta['offsite_dir']} — zkontroluj ho, "
              "nebo nastav settings.backup_dir jinam / na \"off\".")
    if not args.quiet:
        print(f"Záloha hotová: {meta['local']} (integrita ok, záznamy sedí)")
        print(f"  mimo stroj: {'ano' if meta['offsite'] else 'ne'} — {meta['offsite_reason']}")
        if meta["removed"]:
            print(f"  uklizeno {len(meta['removed'])} starých lokálních snímků")
    # rc 0: lokální snímek je hotový. Nedostupný cíl mimo stroj není chyba jobu
    # (jinak by plánovač hlásil selhání každou noc); hlásí se varováním výše.


if __name__ == "__main__":
    main()
