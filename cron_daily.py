#!/usr/bin/env python3
"""Denní doplnění Traktu ze Stremia — běží jako cron bez agenta.

Co dělá: stáhne knihovnu Stremia → porovná ji s Traktem → zapíše chybějící filmy
a epizody s ověřeným mapováním → u seriálů s nečitelnou bitovou mapou doplní
mezeru k poslednímu dokoukanému dílu, ale nejvýš MAX_AUTO dílů (větší mezera se
jen ohlásí k rozhodnutí) → zaktualizuje lokální tracker.db.

Co se zapsalo, čte z deníků `last_push.json` / `last_forward.json`, které most
zapisuje při každém běhu — neodhaduje to porovnáváním souborů.

Ticho znamená dobře: vypíše se jen to, co se opravdu zapsalo, co potřebuje
rozhodnutí, nebo co selhalo. Prázdný výstup cron nikam nepošle.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
LAST_PUSH = HERE / "last_push.json"
LAST_FORWARD = HERE / "last_forward.json"
MAX_AUTO = 8
TIMEOUT = 900


def run(*argv: str) -> tuple[int, str]:
    p = subprocess.run([sys.executable, *argv], cwd=HERE,
                       capture_output=True, text=True, timeout=TIMEOUT)
    return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()


def read_json(path: pathlib.Path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def fmt_eps(pairs: list) -> str:
    """S2E1–E3, S3E1 — slije souvislé epizody po řadách."""
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


def main() -> None:
    # deníky z minulého běhu by se daly splést s tímto
    for path in (LAST_PUSH, LAST_FORWARD):
        path.unlink(missing_ok=True)
    # Starý gaps.json taky nesmí přežít: kdyby v tomhle běhu selhal `compare`,
    # `push` by z něj vzal seznam a zapsal do Traktu i to, co tam už je —
    # a Trakt duplicitní sledování nevyřazuje.
    (HERE / "gaps.json").unlink(missing_ok=True)

    problems: list[str] = []
    ready = True
    for step in (("fetch",), ("compare",)):
        rc, out = run("stremio_bridge.py", *step)
        if rc != 0:
            problems.append(f"{step[0]} selhalo: {out.splitlines()[-1] if out else 'bez výstupu'}")
            ready = False

    if ready:
        for step in (("push", "--yes"), ("forward", "--max", str(MAX_AUTO), "--yes")):
            rc, out = run("stremio_bridge.py", *step)
            if rc != 0:
                problems.append(f"{step[0]} selhalo: "
                                f"{out.splitlines()[-1] if out else 'bez výstupu'}")
    else:
        problems.append("push a forward přeskočeno — bez čerstvého stavu Stremia "
                        "by se do Traktu zapsaly duplicity")

    rc, out = run("track.py", "sync")
    if rc != 0:
        problems.append(f"sync Traktu selhal: {out.splitlines()[-1] if out else 'bez výstupu'}")

    push = read_json(LAST_PUSH, {})
    fwd = read_json(LAST_FORWARD, {})
    new_movies = push.get("movies") or []
    over_max = fwd.get("over_max") or []
    denied = (push.get("denied") or []) + (fwd.get("denied") or [])
    repeat = (push.get("repeat") or []) + (fwd.get("repeat") or [])

    by_show: dict[str, list] = {}
    for row in (push.get("shows") or []) + (fwd.get("written") or []):
        by_show.setdefault(row["name"], []).extend(row["pairs"])

    if not (new_movies or by_show or over_max or denied or repeat or problems):
        return

    today = dt.datetime.now().astimezone().strftime("%-d. %-m.")
    print(f"## Sledování — doplněno do Traktu ({today})")
    print()

    if new_movies:
        titles = ", ".join(m["name"] for m in new_movies)
        unit = "film" if len(new_movies) == 1 else ("filmy" if len(new_movies) < 5 else "filmů")
        print(f"- **{len(new_movies)}** {unit}: {titles}")

    if by_show:
        total = sum(len(v) for v in by_show.values())
        unit = "epizoda" if total == 1 else ("epizody" if total < 5 else "epizod")
        word = "seriálu" if len(by_show) == 1 else "seriálech"
        print(f"- **{total}** {unit} v {len(by_show)} {word}:")
        for show, pairs in sorted(by_show.items(), key=lambda kv: -len(kv[1])):
            print(f"  - {show}: {fmt_eps(pairs)}")

    if over_max:
        print()
        print("**Čeká na tvé rozhodnutí** — mezera je moc velká na automatické doplnění:")
        for row in over_max:
            s, e = row.get("marker") or (0, 0)
            print(f"- {row['name']} — chybí {len(row.get('todo') or [])} dílů, "
                  f"Stremio hlásí dokoukáno do S{s}E{e}. Napiš, jestli to doplnit.")

    if denied:
        print()
        print("⚠ **Trakt něco nepřijal:**")
        for d in denied:
            print(f"- {d}")

    if repeat:
        print()
        print("⚠ **Opakovaný zápis zastaven** — Trakt položku přijal, ale pořád ji nevidí "
              "(skrytá řada, sloučené ID?). Nezapsáno, prověř ručně:")
        for r in repeat:
            print(f"- {r.get('name') or '?'} ({r.get('key')})")

    if problems:
        print()
        print("⚠ **Nepovedlo se:**")
        for p in problems:
            print(f"- {p}")


if __name__ == "__main__":
    main()
