#!/usr/bin/env python3
"""Týdenní rekapitulace sledování — běží jako cron bez agenta.

Čte tracker.db (kterou denně aktualizuje cron_daily.py) a vypíše posledních
sedm dní: filmy, seriály, hodiny, kde jsme skončili, plus souhrn za letošní rok.
"""
from __future__ import annotations

import datetime as dt
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import track  # noqa: E402  (leží ve stejné složce)


def hours(minutes: int) -> str:
    return f"{minutes // 60} h {minutes % 60} min"


def main() -> None:
    now = dt.datetime.now().astimezone()
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = day - dt.timedelta(days=6)
    end = day + dt.timedelta(days=1)
    lo, hi = start.isoformat(), end.isoformat()

    con = track.db()
    row = con.execute("SELECT value FROM meta WHERE key='synced_at'").fetchone()
    synced = row[0] if row else None

    movies = con.execute(
        "SELECT title, year, watched_at, runtime FROM watched_movies "
        "WHERE watched_at>=? AND watched_at<? ORDER BY watched_at", (lo, hi)).fetchall()
    eps = con.execute(
        "SELECT show_title, season, episode, ep_title, watched_at, runtime FROM watched_episodes "
        "WHERE watched_at>=? AND watched_at<? ORDER BY watched_at", (lo, hi)).fetchall()

    minutes = sum(r[3] or 0 for r in movies) + sum(e[5] or 0 for e in eps)
    days: set = set()
    for r in movies:
        p = track.parse_when(r[2])
        if p:
            days.add(p.date())
    for e in eps:
        p = track.parse_when(e[4])
        if p:
            days.add(p.date())

    fmt = "%-d. %-m."
    print(f"## Týden ve sledování ({start.strftime(fmt)}–{now.strftime(fmt)})")
    print()

    if not movies and not eps:
        print("Tento týden nic nového.")
    else:
        parts = []
        if movies:
            parts.append(f"**{len(movies)}** {'film' if len(movies) == 1 else ('filmy' if len(movies) < 5 else 'filmů')}")
        if eps:
            parts.append(f"**{len(eps)}** {'epizoda' if len(eps) == 1 else ('epizody' if len(eps) < 5 else 'epizod')}")
        line = " a ".join(parts)
        if minutes:
            line += f", cca {hours(minutes)}"
        if days:
            line += f", koukáno {len(days)} {'den' if len(days) == 1 else 'dní'}"
        print(f"- {line}")

        if movies:
            agg: dict[tuple[str, int], int] = {}
            for title, year, _w, _r in movies:
                agg[(title, year)] = agg.get((title, year), 0) + 1
            titles = ", ".join(f"{t} ({y})" + (f" {n}×" if n > 1 else "")
                               for (t, y), n in agg.items())
            print(f"- filmy: {titles}")

        if eps:
            from cron_daily import fmt_eps
            by_show: dict[str, list[tuple[int, int]]] = {}
            for show, s, e, _t, _w, _r in eps:
                by_show.setdefault(show, []).append((s, e))
            print("- seriály:")
            for show, pairs in sorted(by_show.items(), key=lambda kv: -len(kv[1])):
                print(f"  - {show}: {len(pairs)}× ({fmt_eps(pairs)})")
    print()

    unfinished = con.execute(
        "SELECT title, aired, completed, last_watched_at, next_season, next_number "
        "FROM shows_progress WHERE completed < aired AND aired > 0 "
        "ORDER BY last_watched_at DESC LIMIT 5").fetchall()
    if unfinished:
        print("**Kde jsme skončili**")
        for title, aired, completed, lw, ns, nn in unfinished:
            nxt = f", další S{ns}E{nn}" if ns else ""
            p = track.parse_when(lw)
            when = f" (naposledy {p.strftime(fmt)})" if p else ""
            print(f"- {title} — {completed}/{aired}{nxt}{when}")
        print()

    year_start = dt.datetime(now.year, 1, 1, tzinfo=now.tzinfo).isoformat()
    y_movies = con.execute("SELECT COUNT(*), SUM(runtime) FROM watched_movies "
                           "WHERE watched_at>=?", (year_start,)).fetchone()
    y_eps = con.execute("SELECT COUNT(*), SUM(runtime) FROM watched_episodes "
                        "WHERE watched_at>=?", (year_start,)).fetchone()
    y_min = (y_movies[1] or 0) + (y_eps[1] or 0)
    print(f"**Letos** ({now.year}): {y_movies[0]} filmů, {y_eps[0]} epizod, cca {hours(y_min)}.")
    if synced:
        print()
        print(f"*data k {synced}*")
    con.close()


if __name__ == "__main__":
    main()
