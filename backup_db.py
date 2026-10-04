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

Cíl se dá nastavit i proměnnou TRAKT_BACKUP_DIR (kvůli cronu) nebo klíčem
"backup_dir" v bloku "settings" v config.json.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import pathlib
import re
import shutil
import sqlite3
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


def target_dir(arg: str | None) -> pathlib.Path:
    if arg:
        return pathlib.Path(arg).expanduser()
    env = os.environ.get("TRAKT_BACKUP_DIR") or common.settings().get("backup_dir")
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
        before = _counts(src)
        src.execute("VACUUM INTO ?", (str(tmp),))
        after = _counts(src)
    finally:
        src.close()

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
    os.chmod(tmp, 0o600)
    tmp.replace(out)

    for name in COPY_ALSO:
        src_file = HERE / name
        if src_file.exists():
            shutil.copy2(src_file, dst / name)

    meta = {"db_records": got[0] + got[1],
            "created": dt.datetime.now().isoformat(timespec="seconds")}
    common.atomic_write_json(dst / "last_backup.json", meta)
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
