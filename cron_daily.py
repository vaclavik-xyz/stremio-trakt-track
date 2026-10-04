#!/usr/bin/env python3
"""Denní doplnění Traktu ze Stremia — pro cron, bez interakce.

Co dělá: ověří přihlášení k Traktu (a token obnoví s předstihem) → stáhne knihovnu
Stremia → porovná ji s Traktem → zapíše chybějící filmy a epizody s ověřeným
mapováním → u seriálů s neověřitelnou bitovou mapou doplní mezeru k poslednímu
dokoukanému dílu, ale nejvýš `max_auto` dílů → zaktualizuje lokální tracker.db
→ zapíše `last_success.json` (a volitelně pingne `heartbeat_url`).

Ticho znamená, že všechno proběhlo. Vypíše se jen to, co se zapsalo, a problémy:
nový neověřený seriál, opakovaný zápis, odmítnutý zápis, selhaný krok, rozbité
přihlášení, stojící tracking. Problém, který už byl nahlášen a nezměnil se, se
zopakuje až po `issue_remind_days` dnech.

Děti se spouštějí s TRAKT_TRACKER_NONINTERACTIVE=1 a stdin z /dev/null: žádný krok
nesmí čekat na člověka (device flow, heslo) — místo toho skončí s EXIT_AUTH.
"""
from __future__ import annotations

import datetime as dt
import os
import pathlib
import subprocess
import sys
import urllib.request

CODE_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(CODE_DIR))

import common  # noqa: E402

HERE = common.DATA_DIR
LAST_PUSH = HERE / "last_push.json"
LAST_FORWARD = HERE / "last_forward.json"
GAPS = HERE / "gaps.json"
ISSUES = HERE / "issues_state.json"
AUTH_STATE = HERE / "auth_state.json"
TIMEOUT = 900
OK_CODES = (common.EXIT_OK, common.EXIT_WARN)

FIX_TRAKT = "spusť v terminálu: python3 track.py auth"
FIX_STREMIO = "spusť v terminálu: bash setup_stremio.sh — do té doby se nic nedoplňuje"


def run(*argv: str, lock_fd: int | None = None) -> tuple[int, str]:
    env = {**os.environ, common.NONINTERACTIVE_ENV: "1"}
    pass_fds: tuple = ()
    if lock_fd is not None:
        env[common.LOCK_ENV] = str(lock_fd)
        pass_fds = (lock_fd,)
    try:
        p = subprocess.run([sys.executable, *argv], cwd=CODE_DIR, env=env, pass_fds=pass_fds,
                           stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           timeout=TIMEOUT)
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        return 124, f"{last_line(out.strip())} (timeout {TIMEOUT} s)"
    return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()


def last_line(out: str) -> str:
    return out.splitlines()[-1] if out else "bez výstupu"


def issues_to_report(current: dict[str, str], now: float | None = None,
                     remind_days: int | None = None, keep_prefixes: tuple = ()) -> list[str]:
    """Vrátí klíče problémů, které je dnes potřeba ohlásit, a uloží stav.

    Ohlásí se problém nový nebo změněný (jiný text) a pak každých `remind_days`
    dní, dokud trvá. Co zmizí, se ze stavu vyřadí — když se vrátí, je zase nové.
    Klíče s prefixem z `keep_prefixes` se při nepřítomnosti nemažou: jejich zdroj
    v tomhle běhu nevznikl (např. compare selhal), takže o nich nic nevíme.
    """
    now = common.now() if now is None else now
    remind = max(1, int(remind_days or common.settings()["issue_remind_days"]))
    state = common.read_json(ISSUES, {})
    out, new_state = [], {}
    for key, text in sorted(current.items()):
        prev = state.get(key)
        if not isinstance(prev, dict):            # nový, nebo stav ze starší verze
            prev = None
        if prev is None or prev.get("text") != text:
            new_state[key] = {"text": text, "first": now, "last": now}
            out.append(key)
            continue
        new_state[key] = prev
        if (now - float(prev.get("last", now))) >= remind * 86400:
            prev["last"] = now
            out.append(key)
    for key, prev in state.items():
        if key not in new_state and key.startswith(keep_prefixes):
            new_state[key] = prev
    common.atomic_write_json(ISSUES, new_state)
    return out


def auth_issue() -> str | None:
    """Text problému z auth_state.json: obnova tokenu selhala, token ještě platí."""
    state = common.read_json(AUTH_STATE, {})
    if not state.get("refresh_failed_at"):
        return None
    left = ""
    try:
        import json
        tok = (json.loads((HERE / "config.json").read_text()).get("token") or {})
        days = (float(tok.get("expires_at") or 0) - common.now()) / 86400
        left = f", token vyprší za {max(days, 0):.0f} d" if days > 0 else ", token už vypršel"
    except (OSError, ValueError):
        pass
    return (f"Obnovení Trakt tokenu selhalo ({state.get('refresh_error')}){left} — {FIX_TRAKT}")


def step_problem(name: str, rc: int, out: str) -> str:
    if rc == common.EXIT_AUTH:
        fix = FIX_STREMIO if name in ("fetch", "compare", "push", "forward") \
            and "Stremio" in out else FIX_TRAKT
        return f"{name}: přihlášení nefunguje ({last_line(out)}) — {fix}"
    return f"{name} selhalo: {last_line(out)}"


def ping_heartbeat() -> None:
    url = common.settings().get("heartbeat_url")
    if not url:
        return
    try:
        urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "stremio-trakt"}),
                               timeout=10).read()
    except Exception:            # noqa: BLE001 — chybějící ping ohlásí hlídací služba sama
        pass


def main() -> None:
    with common.lock() as fd:
        daily(fd)


def daily(lock_fd: int) -> None:
    stale = common.stale_message()           # spočítat dřív, než ho tenhle běh přepíše

    # deníky z minulého běhu by se daly splést s tímto
    for path in (LAST_PUSH, LAST_FORWARD):
        path.unlink(missing_ok=True)
    # Starý gaps.json taky nesmí přežít: kdyby v tomhle běhu selhal `compare`,
    # `push` by z něj vzal seznam a zapsal do Traktu i to, co tam už je —
    # a Trakt duplicitní sledování nevyřazuje.
    GAPS.unlink(missing_ok=True)

    problems: dict[str, str] = {}
    steps_ok: list[str] = []
    ready = True
    for name, argv in (("auth-check", ("track.py", "auth-check")),
                       ("fetch", ("stremio_bridge.py", "fetch")),
                       ("compare", ("stremio_bridge.py", "compare"))):
        rc, out = run(*argv, lock_fd=lock_fd)
        if rc not in OK_CODES:
            problems[f"problem|{name}"] = step_problem(name, rc, out)
            ready = False
            break
        steps_ok.append(name)

    if ready:
        max_auto = str(common.settings()["max_auto"])
        for name, argv in (("push", ("push", "--yes")),
                           ("forward", ("forward", "--max", max_auto, "--yes"))):
            rc, out = run("stremio_bridge.py", *argv, lock_fd=lock_fd)
            if rc not in OK_CODES:
                problems[f"problem|{name}"] = step_problem(name, rc, out)
            else:
                steps_ok.append(name)
    else:
        problems["problem|skipped"] = ("push a forward přeskočeno — bez čerstvého a úplného "
                                       "stavu by se do Traktu zapsaly duplicity")

    rc, out = run("track.py", "sync")
    if rc == common.EXIT_GUARD:
        problems["problem|sync-guard"] = f"sync neprořezal historii: {last_line(out)}"
        steps_ok.append("sync")
    elif rc != common.EXIT_OK:
        problems["problem|sync"] = step_problem("sync", rc, out)
    else:
        steps_ok.append("sync")

    success = all(s in steps_ok for s in ("auth-check", "fetch", "compare", "push", "forward", "sync"))
    if success:
        common.atomic_write_json(common.LAST_SUCCESS, {
            "epoch": common.now(),
            "at": dt.datetime.fromtimestamp(common.now()).astimezone().isoformat(timespec="seconds"),
            "steps": steps_ok})
        ping_heartbeat()
        stale = None
    if stale:
        problems["stale"] = stale
    a = auth_issue()
    if a:
        problems["auth|refresh"] = a

    push = common.read_json(LAST_PUSH, {})
    fwd = common.read_json(LAST_FORWARD, {})
    gaps = common.read_json(GAPS, {})
    new_movies = push.get("movies") or []

    current: dict[str, str] = dict(problems)
    for u in gaps.get("unknown") or []:
        current[f"unknown|{u.get('imdb') or u.get('name')}"] = \
            f"{u.get('name')}: neověřeno — {u.get('reason')}"
    for s in fwd.get("skipped") or []:
        current[f"skipped|{s.get('imdb') or s.get('name')}"] = \
            f"{s.get('name')}: forward nejde — {s.get('reason')}"
    for row in fwd.get("over_max") or []:
        s, e = row.get("marker") or (0, 0)
        current[f"over_max|{row.get('imdb')}"] = (
            f"{row['name']} — chybí {len(row.get('todo') or [])} dílů, Stremio hlásí dokoukáno "
            f"do S{s}E{e}. Doplnit ručně: python3 stremio_bridge.py forward --only {row.get('imdb')} --yes")
    for d in (push.get("denied") or []) + (fwd.get("denied") or []):
        current[f"denied|{d}"] = d
    for r in (push.get("repeat") or []) + (fwd.get("repeat") or []):
        current[f"repeat|{r.get('key')}"] = f"{r.get('name') or '?'} ({r.get('key')})"

    # zdroje, které tenhle běh nevytvořil, nechej ve stavu beze změny
    keep: tuple = ()
    if not GAPS.exists():
        keep += ("unknown|",)
    if not LAST_FORWARD.exists():
        keep += ("skipped|", "over_max|", "repeat|", "denied|")
    elif not LAST_PUSH.exists():
        keep += ("repeat|", "denied|")
    due = set(issues_to_report(current, keep_prefixes=keep))

    by_show: dict[str, list] = {}
    for row in (push.get("shows") or []) + (fwd.get("written") or []):
        by_show.setdefault(row["name"], []).extend(row["pairs"])

    if not (new_movies or by_show or due):
        return

    def section(prefix: str, title: str) -> None:
        keys = sorted(k for k in due if k.startswith(prefix))
        if keys:
            print()
            print(title)
            for k in keys:
                print(f"- {current[k]}")

    today = dt.datetime.fromtimestamp(common.now()).astimezone().strftime("%-d. %-m.")
    print(f"## Sledování ({today})")
    if new_movies or by_show:
        print()
    if new_movies:
        titles = ", ".join(m["name"] for m in new_movies)
        print(f"- doplněno **{common.cn(len(new_movies), 'film', 'filmy', 'filmů')}**: {titles}")
    if by_show:
        total = sum(len(v) for v in by_show.values())
        word = "seriálu" if len(by_show) == 1 else "seriálech"
        print(f"- doplněno **{common.cn(total, 'epizoda', 'epizody', 'epizod')}** "
              f"v {len(by_show)} {word}:")
        for show, pairs in sorted(by_show.items(), key=lambda kv: -len(kv[1])):
            print(f"  - {show}: {common.fmt_eps(pairs)}")

    section("stale", "🛑 **Tracking stojí:**")
    section("problem|", "⚠ **Nepovedlo se:**")
    section("auth|", "⚠ **Přihlášení:**")
    section("repeat|", "⚠ **Opakovaný zápis zastaven** — Trakt položku přijal, ale pořád ji "
                       "nevidí (skrytá řada, sloučené ID?). Nezapsáno, prověř ručně:")
    section("denied|", "⚠ **Trakt něco nepřijal:**")
    section("over_max|", "**Čeká na tvé rozhodnutí** — mezera je moc velká na automatické doplnění:")
    section("unknown|", "**Nesynchronizuje se** (most to za tebe neudělá):")
    section("skipped|", "**Forward nejde:**")


if __name__ == "__main__":
    main()
