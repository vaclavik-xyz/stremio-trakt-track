#!/usr/bin/env python3
"""Záloha tracker.db do iCloudu — konzistentní snapshot, ne živá databáze.

Živou SQLite databázi nedávej rovnou do iCloudu: synchronizace na úrovni souborů
umí databázi rozbít (dva zápisy proti sobě, zámky, konfliktní kopie) a iCloud
navíc nerozlišuje, která strana je ta správná. Místo toho se z databáze udělá
konzistentní kopie přes `VACUUM INTO` a do iCloudu jde ta — databáze zůstane
lokální a v iCloudu jsou verze, takže se dá vrátit i něco, co se omylem smazalo
z Traktu.

Použití:
    python3 backup_db.py            # snapshot + úklid starých
    python3 backup_db.py --list     # co je v záloze
    python3 backup_db.py --to DIR   # jiná cílová složka

Cíl se dá nastavit i proměnnou TRAKT_BACKUP_DIR (kvůli cronu).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import shutil
import sqlite3
import sys

HERE = pathlib.Path(__file__).resolve().parent
DB = HERE / "tracker.db"
ICLOUD = (pathlib.Path.home() / "Library/Mobile Documents/com~apple~CloudDocs"
          / "trakt-tracker-backups")
COPY_ALSO = ["alias.json"]          # co je malé a nenahraditelné
KEEP_DAYS = 30                      # denní snapshots za posledních 30 dní
KEEP_MONTHS = 12                    # + poslední snapshot z každého měsíce


def target_dir(arg: str | None) -> pathlib.Path:
    if arg:
        return pathlib.Path(arg).expanduser()
    env = os.environ.get("TRAKT_BACKUP_DIR")
    if env:
        return pathlib.Path(env).expanduser()
    return ICLOUD


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
        out = dst / f"tracker-{stamp}-{dt.datetime.now().strftime('%H%M')}.sqlite"
    tmp = out.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    src = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        src.execute("VACUUM INTO ?", (str(tmp),))
    finally:
        src.close()
    tmp.replace(out)

    # kontrola: kopie se musí dát otevřít a mít stejně záznamů jako originál
    with sqlite3.connect(DB) as a, sqlite3.connect(out) as b:
        check = b.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            out.unlink(missing_ok=True)
            sys.exit(f"Snímek neprošel kontrolou integrity ({check}) — zahozen.")
        for table in ("watched_movies", "watched_episodes", "shows_progress"):
            n_src = a.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            n_dst = b.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            if n_src != n_dst:
                out.unlink(missing_ok=True)
                sys.exit(f"Snímek nesedí ({table}: {n_src} vs {n_dst}) — zahozen.")

    for name in COPY_ALSO:
        src_file = HERE / name
        if src_file.exists():
            shutil.copy2(src_file, dst / name)

    meta = {"db_records": None, "created": dt.datetime.now().isoformat(timespec="seconds")}
    with sqlite3.connect(out) as con:
        meta["db_records"] = sum(
            con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("watched_movies", "watched_episodes"))
    (dst / "last_backup.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1))
    return out


def prune(dst: pathlib.Path) -> list[pathlib.Path]:
    """Nechá všechny snapshots za posledních 30 dní + nejnovější z každého měsíce."""
    snaps = snapshots(dst)
    if not snaps:
        return []
    cut = dt.date.today() - dt.timedelta(days=KEEP_DAYS)
    keep = {s for s in snaps if dt.date.fromtimestamp(s.stat().st_mtime) >= cut}
    monthly: dict[str, pathlib.Path] = {}
    for s in snaps:
        key = dt.date.fromtimestamp(s.stat().st_mtime).strftime("%Y-%m")
        monthly[key] = s          # snaps jsou seřazené, takže vyjde nejnovější z měsíce
    keep |= set(list(monthly.values())[-KEEP_MONTHS:])
    if snaps:
        keep.add(snaps[-1])
    removed = []
    for s in snaps:
        if s not in keep:
            s.unlink()
            removed.append(s)
    return removed


def main() -> None:
    ap = argparse.ArgumentParser(description="Záloha tracker.db do iCloudu")
    ap.add_argument("--to", help="cílová složka (výchozí iCloud, jinak $TRAKT_BACKUP_DIR)")
    ap.add_argument("--list", action="store_true", help="jen vypsat, co je v záloze")
    ap.add_argument("--quiet", action="store_true",
                    help="nic nevypisovat, když záloha projde (pro cron)")
    args = ap.parse_args()
    dst = target_dir(args.to)

    if args.list:
        snaps = snapshots(dst)
        if not snaps:
            print(f"V {dst} nic není.")
            return
        total = sum(s.stat().st_size for s in snaps)
        print(f"{dst} — {len(snaps)} snapshots, celkem {total / 1024:.0f} kB")
        for s in snaps:
            when = dt.datetime.fromtimestamp(s.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            print(f"  {s.name:32s} {when}  {s.stat().st_size / 1024:.0f} kB")
        return

    out = make_snapshot(dst)
    removed = prune(dst)
    size = sum(s.stat().st_size for s in snapshots(dst))
    if args.quiet:
        return                      # ticho = dobře; chyby hlásí make_snapshot na stderr
    print(f"Záloha hotová: {out}")
    print(f"  kontrola: integrita ok, záznamy sedí s tracker.db")
    print(f"  v záloze teď {len(snapshots(dst))} snapshots, {size / 1024:.0f} kB")
    if removed:
        print(f"  uklizeno {len(removed)} starých: {', '.join(s.name for s in removed)}")


if __name__ == "__main__":
    main()
