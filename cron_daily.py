#!/usr/bin/env python3
"""Denní doplnění Traktu ze Stremia — pro cron, bez interakce.

Co dělá: stáhne knihovnu Stremia → porovná ji s Traktem → zapíše chybějící filmy
a epizody s ověřeným mapováním → u seriálů s neověřitelnou bitovou mapou doplní
mezeru k poslednímu dokoukanému dílu, ale nejvýš `max_auto` dílů (větší mezera se
jen ohlásí k rozhodnutí) → zaktualizuje lokální tracker.db.

Co se zapsalo, čte z deníků `last_push.json` / `last_forward.json`, které most
zapisuje průběžně — neodhaduje to porovnáváním souborů.

Ticho znamená dobře: vypíše se jen to, co se opravdu zapsalo, co potřebuje
rozhodnutí, nebo co selhalo. Prázdný výstup cron nikam nepošle. Seriály, které
nejdou ověřit, se hlásí, když se objeví nově, a pak jednou za `issue_remind_days`.
"""
from __future__ import annotations

import datetime as dt
import os
import pathlib
import subprocess
import sys
import time

CODE_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(CODE_DIR))

import common  # noqa: E402

HERE = common.DATA_DIR
LAST_PUSH = HERE / "last_push.json"
LAST_FORWARD = HERE / "last_forward.json"
GAPS = HERE / "gaps.json"
ISSUES = HERE / "issues_state.json"
TIMEOUT = 900
OK_CODES = (common.EXIT_OK, common.EXIT_WARN)


def run(*argv: str, lock_fd: int | None = None) -> tuple[int, str]:
    env = dict(os.environ)
    pass_fds: tuple = ()
    if lock_fd is not None:
        env[common.LOCK_ENV] = str(lock_fd)
        pass_fds = (lock_fd,)
    try:
        p = subprocess.run([sys.executable, *argv], cwd=CODE_DIR, env=env, pass_fds=pass_fds,
                           capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        return 124, f"{last_line(out.strip())} (timeout {TIMEOUT} s)"
    return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()


def last_line(out: str) -> str:
    return out.splitlines()[-1] if out else "bez výstupu"


def issues_to_report(current: dict[str, str], now: float | None = None,
                     remind_days: int | None = None) -> list[str]:
    """Vrátí texty problémů, které je dnes potřeba ohlásit, a uloží stav.

    Ohlásí se problém nový a pak každých `remind_days` dní, dokud trvá; co zmizí,
    se ze stavu vyřadí (když se vrátí, je zase nový)."""
    now = time.time() if now is None else now
    remind = int(remind_days or common.settings()["issue_remind_days"])
    state = common.read_json(ISSUES, {})
    out, new_state = [], {}
    for key, text in sorted(current.items()):
        first = float(state.get(key, now))
        new_state[key] = first
        age = int((now - first) // 86400)
        if key not in state:
            out.append(f"{text} (nové)")
        elif age > 0 and age % remind == 0:
            out.append(f"{text} (trvá {age} dní)")
    common.atomic_write_json(ISSUES, new_state)
    return out


def main() -> None:
    with common.lock() as fd:
        daily(fd)


def daily(lock_fd: int) -> None:
    # deníky z minulého běhu by se daly splést s tímto
    for path in (LAST_PUSH, LAST_FORWARD):
        path.unlink(missing_ok=True)
    # Starý gaps.json taky nesmí přežít: kdyby v tomhle běhu selhal `compare`,
    # `push` by z něj vzal seznam a zapsal do Traktu i to, co tam už je —
    # a Trakt duplicitní sledování nevyřazuje.
    GAPS.unlink(missing_ok=True)

    problems: list[str] = []
    ready = True
    for step in (("fetch",), ("compare",)):
        rc, out = run("stremio_bridge.py", *step, lock_fd=lock_fd)
        if rc not in OK_CODES:
            problems.append(f"{step[0]} selhalo: {last_line(out)}")
            ready = False
            break

    if ready:
        max_auto = str(common.settings()["max_auto"])
        for step in (("push", "--yes"), ("forward", "--max", max_auto, "--yes")):
            rc, out = run("stremio_bridge.py", *step, lock_fd=lock_fd)
            if rc not in OK_CODES:
                problems.append(f"{step[0]} selhalo: {last_line(out)}")
    else:
        problems.append("push a forward přeskočeno — bez čerstvého stavu Stremia "
                        "by se do Traktu zapsaly duplicity")

    rc, out = run("track.py", "sync")
    if rc != common.EXIT_OK:
        problems.append(f"sync Traktu selhal: {last_line(out)}")

    push = common.read_json(LAST_PUSH, {})
    fwd = common.read_json(LAST_FORWARD, {})
    gaps = common.read_json(GAPS, {})
    new_movies = push.get("movies") or []
    over_max = fwd.get("over_max") or []
    denied = (push.get("denied") or []) + (fwd.get("denied") or [])
    repeat = (push.get("repeat") or []) + (fwd.get("repeat") or [])

    current: dict[str, str] = {}
    for u in gaps.get("unknown") or []:
        current[f"unknown|{u.get('imdb') or u.get('name')}"] = \
            f"{u.get('name')}: neověřeno — {u.get('reason')}"
    for s in fwd.get("skipped") or []:
        current[f"skipped|{s.get('imdb') or s.get('name')}"] = \
            f"{s.get('name')}: forward nejde — {s.get('reason')}"
    issues = issues_to_report(current) if ready else []

    by_show: dict[str, list] = {}
    for row in (push.get("shows") or []) + (fwd.get("written") or []):
        by_show.setdefault(row["name"], []).extend(row["pairs"])

    if not (new_movies or by_show or over_max or denied or repeat or issues or problems):
        return

    today = dt.datetime.now().astimezone().strftime("%-d. %-m.")
    print(f"## Sledování — doplněno do Traktu ({today})")
    print()

    if new_movies:
        titles = ", ".join(m["name"] for m in new_movies)
        print(f"- **{common.cn(len(new_movies), 'film', 'filmy', 'filmů')}**: {titles}")

    if by_show:
        total = sum(len(v) for v in by_show.values())
        word = "seriálu" if len(by_show) == 1 else "seriálech"
        print(f"- **{common.cn(total, 'epizoda', 'epizody', 'epizod')}** v {len(by_show)} {word}:")
        for show, pairs in sorted(by_show.items(), key=lambda kv: -len(kv[1])):
            print(f"  - {show}: {common.fmt_eps(pairs)}")

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

    if issues:
        print()
        print("**Nesynchronizuje se** (most to za tebe neudělá):")
        for t in issues:
            print(f"- {t}")

    if problems:
        print()
        print("⚠ **Nepovedlo se:**")
        for p in problems:
            print(f"- {p}")


if __name__ == "__main__":
    main()
