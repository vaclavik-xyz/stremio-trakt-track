#!/usr/bin/env python3
"""Most Stremio → Trakt.

Stremio si svou knihovnu a stav zhlédnutí drží ve vlastním cloudu (to je jeho
základní funkce a nepadá). Trakt propojení ve Stremiu ale odpadá po hodinách.
Tenuhle most čte stav ze Stremia a doplňuje, co v Traktu chybí.

Použití:
  python3 stremio_bridge.py list        # co je ve stremio knihovně
  python3 stremio_bridge.py compare     # co má Stremio zhlédnuté a Trakt ne
  python3 stremio_bridge.py push        # zápis chybějícího do Traktu (dry-run)
  python3 stremio_bridge.py push --yes  # skutečně zapsat

Přihlášení (jednorázově): bash setup_stremio.sh
Ukládá se jen authKey do stremio.json (práva 600), heslo se nikam neukládá.
"""
from __future__ import annotations

import argparse
import base64
import difflib
import json
import os
import pathlib
import re
import sqlite3
import stat
import sys
import time
import urllib.error
import urllib.request
import zlib

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

STREMIO_CFG = HERE / "stremio.json"
RAW = HERE / "stremio_library.json"
PUSHED = HERE / "pushed.json"
CINEMETA_CACHE = HERE / "cinemeta_cache.json"
SAPI = "https://api.strem.io/api"
CINEMETA = "https://v3-cinemeta.strem.io/meta/series/{}.json"
UA = "stremio-trakt-track/1.0.0"


# ------------------------------------------------------------------ pomocné


def load_stremio() -> dict:
    if not STREMIO_CFG.exists():
        sys.exit("Chybí přihlášení ke Stremiu. Spusť: bash setup_stremio.sh")
    return json.loads(STREMIO_CFG.read_text())


def save_stremio(cfg: dict) -> None:
    STREMIO_CFG.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
    os.chmod(STREMIO_CFG, stat.S_IRUSR | stat.S_IWUSR)


def s_call(path: str, payload: dict) -> object:
    req = urllib.request.Request(
        SAPI + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": UA}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        sys.exit(f"Stremio API HTTP {e.code}: {e.read()[:200].decode(errors='replace')}")
    except urllib.error.URLError as e:
        sys.exit(f"Síťová chyba u Stremia: {e.reason}")
    if isinstance(data, dict) and data.get("error"):
        sys.exit(f"Stremio API: {data['error'].get('message')}")
    return data.get("result") if isinstance(data, dict) else data


def cmd__login(args: argparse.Namespace) -> None:
    """Interní – volá ho setup_stremio.sh. Heslo jde po stdin, nikdy do logu."""
    password = sys.stdin.read().strip()
    if not password:
        sys.exit("Prázdné heslo.")
    result = s_call("/login", {"email": args.email, "password": password})
    auth_key = (result or {}).get("authKey")
    if not auth_key:
        sys.exit("Přihlášení nevrátilo klíč – zkontroluj e-mail a heslo.")
    save_stremio({"email": args.email, "authKey": auth_key})
    user = (result or {}).get("user") or {}
    print(f"Přihlášeno jako {user.get('email') or args.email}. Klíč uložen do stremio.json (600).")


# ------------------------------------------------------------- stremio data


def fetch_raw(force: bool = False, max_age: int = 300) -> list:
    if not force and RAW.exists() and time.time() - RAW.stat().st_mtime < max_age:
        return json.loads(RAW.read_text())
    cfg = load_stremio()
    items = s_call("/datastoreGet",
                   {"authKey": cfg["authKey"], "collection": "libraryItem", "all": True}) or []
    RAW.write_text(json.dumps(items, indent=1, ensure_ascii=False))
    return items


def decode_watched(text: str):
    """Rozbalí bitovou mapu Stremia: <id>:<poslSeason>:<poslEp>:<N>:<b64(zlib(bitset))>.

    Pozor: <poslSeason>/<poslEp> je poslední DOKOUKANÝ díl (Stremio ho do bitové mapy
    nezahrnuje, dokud díl není dotažený), <bitset> je počet dílů před ním.
    """
    sid, season, ep, n_str, payload = text.split(":", 4)
    n = int(n_str)
    try:
        raw = zlib.decompress(base64.b64decode(payload))
    except Exception:
        return sid, int(season), int(ep), n, []
    idx = [i for i in range(n) if i // 8 < len(raw) and raw[i // 8] & (1 << (i % 8))]
    return sid, int(season), int(ep), n, idx


def cinemeta_videos(imdb: str) -> list[tuple[int, int, str]]:
    cache = json.loads(CINEMETA_CACHE.read_text()) if CINEMETA_CACHE.exists() else {}
    if imdb in cache:
        return [tuple(x) for x in cache[imdb]]
    try:
        with urllib.request.urlopen(CINEMETA.format(imdb), timeout=25) as r:
            meta = (json.loads(r.read()) or {}).get("meta") or {}
    except Exception as e:
        print(f"  ! metadata pro {imdb} se nepovedla: {e}", file=sys.stderr)
        return []
    videos = [(v.get("season"), v.get("number"), v.get("name") or "") for v in (meta.get("videos") or [])]
    cache[imdb] = videos
    CINEMETA_CACHE.write_text(json.dumps(cache, ensure_ascii=False))
    return videos


def stremio_state(item: dict) -> dict:
    """Vrátí stav zhlédnutí. U seriálů ověří, že pořadí epizod sedí."""
    state = item.get("state") or {}
    kind = item.get("type")
    last = state.get("lastWatched")
    plays = int(state.get("timesWatched") or 0)
    out = {"watched": set(), "last": last, "plays": plays, "kind": kind,
           "name": item.get("name"), "verified": True}
    if kind == "series":
        w = state.get("watched")
        if w:
            sid, ls, le, n, idx = decode_watched(w)
            videos = cinemeta_videos(sid or item.get("_id"))
            if len(videos) >= n and n:
                for i in idx:
                    s, e, _t = videos[i]
                    if s is not None and e is not None:
                        out["watched"].add((int(s), int(e)))
                # Stremio sám říká, který díl byl poslední → musí sedět na nejvyšší index
                if idx:
                    s, e, _t = videos[max(idx)]
                    out["marker"] = (ls, le)
                    out["marker_mapped"] = (int(s), int(e))
                    if (int(s), int(e)) != (ls, le) and (ls, le) != (0, 0):
                        out["verified"] = False
                        out["reason"] = (f"pořadí nesedí: Stremio říká S{ls}E{le}, "
                                         f"mapování dává S{s}E{e}")
            else:
                out["verified"] = False
                out["error"] = f"metadata nesedí ({len(videos)} videí vs {n} v mapě)"
        vid = state.get("video_id")
        if vid and ":" in vid:
            parts = vid.split(":")
            if len(parts) >= 3:
                try:
                    out["last_ep"] = (int(parts[-2]), int(parts[-1]))
                except ValueError:
                    pass
    else:
        if plays > 0 or state.get("flaggedWatched") or last:
            out["watched"].add((0, 0))
    return out


def trakt_episode_inventory(imdb: str) -> set[tuple[int, int]]:
    """Všechny epizody, které Trakt u seriálu zná (pro kontrolu, že mapování dává smysl)."""
    cache_path = HERE / "trakt_episodes_cache.json"
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    if imdb in cache:
        return {tuple(x) for x in cache[imdb]}
    import track
    try:
        seasons = track._req("GET", f"/shows/{imdb}/seasons", params={"extended": "episodes"})[0]
    except Exception as e:
        print(f"  ! seznam epizod pro {imdb} selhal: {e}", file=sys.stderr)
        return set()
    inv = set()
    for season in seasons or []:
        for ep in season.get("episodes") or []:
            inv.add((int(season["number"]), int(ep["number"])))
    cache[imdb] = sorted(inv)
    cache_path.write_text(json.dumps(cache, ensure_ascii=False))
    return inv


def trakt_watched_episodes(imdb: str) -> set[tuple[int, int]]:
    import track
    try:
        pr = track._req("GET", f"/shows/{imdb}/progress/watched",
                        params={"hidden": "false", "specials": "true"})[0]
    except Exception as e:
        print(f"  ! Trakt pro {imdb} selhal: {e}", file=sys.stderr)
        return set()
    done = set()
    for season in pr.get("seasons") or []:
        for ep in season.get("episodes") or []:
            if ep.get("completed"):
                done.add((int(season["number"]), int(ep["number"])))
    return done


def load_alias() -> dict:
    """Některé tituly má Trakt pod jiným IMDb ID než Stremio → alias.json."""
    p = HERE / "alias.json"
    if not p.exists():
        return {}
    data = json.loads(p.read_text())
    return {k: v for k, v in data.items() if not k.startswith("_")}


def trakt_watched_movies() -> set[str]:
    """Zhlédnuté filmy přímo z Traktu (ne z lokální DB, ta může být starší)."""
    import track
    try:
        rows = track.paged("/sync/history/movies", {"extended": "full"})
        ids = {((r.get("movie") or {}).get("ids") or {}).get("imdb") for r in rows}
        return {i for i in ids if i}
    except Exception as e:
        print(f"  ! historii filmů z Traktu nešlo přečíst ({e}), beru lokální DB", file=sys.stderr)
        con = sqlite3.connect(HERE / "tracker.db")
        try:
            rows = con.execute("SELECT imdb FROM watched_movies WHERE imdb IS NOT NULL").fetchall()
        except sqlite3.OperationalError:
            rows = []
        finally:
            con.close()
        return {r[0] for r in rows}


# ------------------------------------------------------------------ příkazy


def cmd_list(_args: argparse.Namespace) -> None:
    items = fetch_raw()
    print(f"Knihovna Stremia: {len(items)} položek")
    series = [i for i in items if i.get("type") == "series"]
    movies = [i for i in items if i.get("type") != "series"]
    print(f"  seriály: {len(series)} | filmy a ostatní: {len(movies)}")
    print()
    print("Seriály:")
    for it in sorted(series, key=lambda x: (x.get("state") or {}).get("lastWatched") or "", reverse=True):
        st = stremio_state(it)
        err = f"  ⚠ {st['error']}" if st.get("error") else ""
        print(f"  {it.get('name')} — {len(st['watched'])} epizod, naposledy {st['last'] or '?'}{err}")
    print()
    print("Filmy:")
    for it in sorted(movies, key=lambda x: (x.get("state") or {}).get("lastWatched") or "", reverse=True)[:20]:
        st = stremio_state(it)
        print(f"  {it.get('name')} — {'zhlédnuto' if st['watched'] else 'nezhlédnuto'}, naposledy {st['last'] or '?'}")


def compute_gaps() -> dict:
    items = fetch_raw()
    alias = load_alias()
    trakt_movies = trakt_watched_movies()
    gaps = {"movies": [], "shows": [], "unknown": []}

    for it in items:
        st = stremio_state(it)
        imdb = it.get("_id") or ""
        if st.get("error"):
            gaps["unknown"].append({"name": st["name"], "reason": st["error"]})
            continue
        if not st["watched"]:
            continue
        trakt_id = alias.get(imdb, imdb)
        trakt_movies = {alias.get(m, m) for m in trakt_movies} if alias else trakt_movies
        if st["kind"] == "series":
            if not st.get("verified"):
                gaps["unknown"].append({"name": st["name"], "reason": st.get("reason") or "neověřeno"})
                continue
            inventory = trakt_episode_inventory(trakt_id)
            bogus = sorted(p for p in st["watched"] if inventory and p not in inventory)
            if bogus:
                gaps["unknown"].append({
                    "name": st["name"],
                    "reason": f"mapování dává neexistující epizody ({len(bogus)}), např. "
                              + ", ".join(f"S{a}E{b}" for a, b in bogus[:3])})
                continue
            done = trakt_watched_episodes(trakt_id)
            missing = sorted(st["watched"] - done)
            if missing:
                gaps["shows"].append({
                    "imdb": trakt_id, "name": st["name"], "last": st["last"],
                    "last_ep": st.get("last_ep"),
                    "missing": missing, "trakt_has": len(done),
                    "stremio_has": len(st["watched"]),
                })
        else:
            if imdb and trakt_id not in trakt_movies:
                gaps["movies"].append({"imdb": trakt_id, "name": st["name"], "last": st["last"]})
    return gaps


def cmd_fetch(_args: argparse.Namespace) -> None:
    items = fetch_raw(force=True)
    print(f"Staženo {len(items)} položek z Stremio knihovny → stremio_library.json")


def cmd_compare(_args: argparse.Namespace) -> None:
    gaps = compute_gaps()
    print("== Rozdíl: co má Stremio zhlédnuté, ale Trakt ne ==")
    if gaps["movies"]:
        print(f"\nFilmy ({len(gaps['movies'])}):")
        for m in gaps["movies"]:
            print(f"  {m['name']} — naposledy {m['last'] or '?'}")
    if gaps["shows"]:
        print(f"\nSeriály ({len(gaps['shows'])}):")
        for s in gaps["shows"]:
            eps = ", ".join(f"S{a}E{b}" for a, b in s["missing"][:14])
            more = f" …(+{len(s['missing']) - 14})" if len(s["missing"]) > 14 else ""
            print(f"  {s['name']}: chybí {len(s['missing'])} epizod ({eps}{more})"
                  f" | v Traktu {s['trakt_has']}, ve Stremiu {s['stremio_has']}")
    if gaps["unknown"]:
        print(f"\nNepíšu do Traktu (mapování neověřeno, {len(gaps['unknown'])}):")
        for u in gaps["unknown"]:
            print(f"  {u['name']}: {u['reason']}")
        print("  → pro tyhle použij `forward`: doplní jen mezeru k poslednímu dokoukanému dílu")
    total = len(gaps["movies"]) + sum(len(s["missing"]) for s in gaps["shows"])
    print(f"\nCelkem chybí {total} položek.")
    (HERE / "gaps.json").write_text(json.dumps(gaps, indent=2, ensure_ascii=False))
    print("Uloženo do gaps.json.")


def cmd_push(args: argparse.Namespace) -> None:
    gaps_path = HERE / "gaps.json"
    if not gaps_path.exists():
        sys.exit("Nejdřív spusť: python3 stremio_bridge.py compare")
    gaps = json.loads(gaps_path.read_text())
    # Nefiltrujeme podle pushed.json: o tom, co chybí, rozhoduje živý stav Traktu
    # (gaps.json). Když se záznam v Traktu ztratí, most ho má doplnit znovu.
    # pushed.json slouží jen pro `redate` (co jsme zapsali bez data).
    movies = list(gaps["movies"])
    shows = []
    skipped_specials = 0
    for s in gaps["shows"]:
        missing = list(s["missing"])
        if not args.include_specials:
            no_spec = [(a, b) for a, b in missing if a != 0]
            skipped_specials += len(missing) - len(no_spec)
            missing = no_spec
        if missing:
            shows.append({**s, "missing": missing})

    print(f"K zápisu: {len(movies)} filmů, {sum(len(s['missing']) for s in shows)} epizod")
    if skipped_specials:
        print(f"  (vynecháno {skipped_specials} speciálů S0 – přidej --include-specials, pokud je chceš)" )
    for m in movies:
        print(f"  film: {m['name']} ({m['last'][:10] if m.get('last') else 'unknown'})")
    for s in shows:
        print(f"  seriál: {s['name']} — {len(s['missing'])} epizod")

    if not args.yes:
        print("\n(Dry-run. Pro skutečný zápis přidej --yes.)")
        return

    import track
    pushed = json.loads(PUSHED.read_text()) if PUSHED.exists() else {"movies": [], "episodes": []}
    journal = {"movies": [], "shows": [], "denied": []}
    # filmy
    if movies:
        payload = {"movies": [{"ids": {"imdb": m["imdb"]},
                               "watched_at": m["last"] or "unknown"} for m in movies]}
        res = track._req("POST", "/sync/history", body=payload)[0]
        added = (res.get("added") or {}).get("movies") or 0
        nf = (res.get("not_found") or {}).get("movies") or []
        print(f"  filmy: zapsáno {added} z {len(movies)} | nenalezeno: {len(nf)}")
        if nf:
            names = [x.get("title") or "?" for x in nf]
            journal["denied"] += names
            print("   nenalezené:", ", ".join(names))
        if added == len(movies):
            pushed["movies"].extend(m["imdb"] for m in movies)
            journal["movies"] = [{"imdb": m["imdb"], "name": m["name"]} for m in movies]
        else:
            journal["denied"].append(f"filmy: Trakt přijal {added} z {len(movies)}")
            print("  ! ne všechno prošlo, do pushed.json neukládám "
                  "(spusť compare a pak push znovu)")
        time.sleep(1.2)

    # epizody – po seriálech, poslední epizoda s přesným datem, starší jako unknown
    for s in shows:
        seasons: dict[int, list] = {}
        last_ep = tuple(s.get("last_ep") or ())
        for a, b in s["missing"]:
            when = s["last"] if (a, b) == last_ep else "unknown"
            seasons.setdefault(a, []).append({"number": b, "watched_at": when or "unknown"})
        payload = {"shows": [{"ids": {"imdb": s["imdb"]},
                              "seasons": [{"number": num, "episodes": eps}
                                          for num, eps in sorted(seasons.items())]}]}
        res = track._req("POST", "/sync/history", body=payload)[0]
        added = (res.get("added") or {}).get("episodes") or 0
        print(f"  {s['name']}: zapsáno {added} z {len(s['missing'])} epizod")
        if added == len(s["missing"]):
            pushed["episodes"].extend([s["imdb"], a, b] for a, b in s["missing"])
            journal["shows"].append({"imdb": s["imdb"], "name": s["name"], "pairs": s["missing"]})
        else:
            journal["denied"].append(f"{s['name']}: {added} z {len(s['missing'])}")
            print(f"  ! {s['name']}: neprošlo všechno (Trakt tenhle seriál možná nezná pod tímhle ID)")
        time.sleep(1.2)

    PUSHED.write_text(json.dumps(pushed, indent=1, ensure_ascii=False))
    (HERE / "last_push.json").write_text(json.dumps(journal, ensure_ascii=False, indent=1))
    print("\nHotovo. Zápis je v pushed.json, aby se nic neposílalo dvakrát.")


def cmd_forward(args: argparse.Namespace) -> None:
    """Doplní Trakt dopředu na díl, který Stremio označuje jako poslední dokoukaný.

    Používá se pro seriály, kde se bitová mapa nedá spolehlivě namapovat (Stremio
    indexuje podle metadat, která se mezitím změnila). Nebere se z ní nic – jen
    z toho, co Stremio samo uvádí jako poslední dokoukaný díl, se doplní mezera
    mezi nejvyšším dílem v Traktu a ním.
    """
    items = fetch_raw()
    alias = load_alias()
    rows, skipped = [], []
    for it in items:
        if it.get("type") != "series":
            continue
        state = it.get("state") or {}
        w = state.get("watched")
        if not w:
            continue
        _sid, se, ep, _n, idx = decode_watched(w)
        imdb = alias.get(it.get("_id"), it.get("_id"))
        inv = sorted(trakt_episode_inventory(imdb))
        if not inv:
            continue
        if (se, ep) not in inv:
            skipped.append(f"{it.get('name')}: Stremio uvádí poslední dokoukaný S{se}E{ep}, "
                           f"takový díl Trakt u {imdb} nezná")
            continue
        done = trakt_watched_episodes(imdb)
        mx = max(done) if done else None
        start = inv.index(mx) + 1 if mx in inv else 0
        todo = [e for e in inv[start:inv.index((se, ep)) + 1] if e not in done]
        if not todo:
            continue
        rows.append({"name": it.get("name"), "imdb": imdb, "marker": (se, ep),
                     "todo": todo, "bits": len(idx), "last": state.get("lastWatched")})

    rows.sort(key=lambda r: len(r["todo"]))
    print(f"== Doplnění dopředu: {len(rows)} seriálů ==")
    for r in rows:
        eps = ", ".join(f"S{a}E{b}" for a, b in r["todo"])
        warn = ""
        if r["bits"] < len(r["todo"]):
            warn = f"  ⚠ Stremio má jen {r['bits']} zhlédnutých dílů – možná některé přeskočil"
        print(f"\n  {r['name']} ({r['imdb']}) — poslední dokoukaný S{r['marker'][0]}E{r['marker'][1]},"
              f" doplnit {len(r['todo'])} dílů, naposledy {str(r['last'])[:10]}{warn}")
        print(f"    {eps[:200]}{' …' if len(eps) > 200 else ''}")
    if skipped:
        print("\n== Nešlo (Trakt ten díl nezná) ==")
        for s in skipped:
            print(f"  {s}")

    (HERE / "forward.json").write_text(json.dumps(
        [r for r in rows if args.only is None or r["imdb"] == args.only],
        indent=2, ensure_ascii=False))
    total = sum(len(r["todo"]) for r in rows if args.only is None or r["imdb"] == args.only)
    print(f"\nCelkem k doplnění: {total} dílů.")
    if not args.yes:
        print("(Dry-run. Pro zápis přidej --yes. Upsat datum: --date unknown, nebo výchozí = den posledního dílu.)")
        return

    import track
    journal = {"written": [], "denied": [], "over_max": []}
    selected = [r for r in rows if args.only is None or r["imdb"] == args.only]
    to_write = [r for r in selected if args.max is None or len(r["todo"]) <= args.max]
    journal["over_max"] = [{"imdb": r["imdb"], "name": r["name"], "todo": r["todo"],
                            "marker": r["marker"]} for r in selected if r not in to_write]
    if args.yes and len(to_write) < len(selected):
        for r in selected:
            if r not in to_write:
                print(f"  {r['name']}: {len(r['todo'])} dílů – přes limit --max={args.max}, "
                      f"nechávám ke schválení (zapiš ručně s --only {r['imdb']})")
    pushed = json.loads(PUSHED.read_text()) if PUSHED.exists() else {"movies": [], "episodes": []}
    for r in to_write:
        when = args.date or r["last"] or "unknown"
        seasons: dict[int, list] = {}
        for a, b in r["todo"]:
            seasons.setdefault(a, []).append({"number": b, "watched_at": when})
        payload = {"shows": [{"ids": {"imdb": r["imdb"]},
                              "seasons": [{"number": n, "episodes": e}
                                          for n, e in sorted(seasons.items())]}]}
        res = track._req("POST", "/sync/history", body=payload)[0]
        added = (res.get("added") or {}).get("episodes") or 0
        print(f"  {r['name']}: zapsáno {added} z {len(r['todo'])}")
        if added == len(r["todo"]):
            pushed["episodes"].extend([r["imdb"], a, b] for a, b in r["todo"])
            journal["written"].append({"imdb": r["imdb"], "name": r["name"], "pairs": r["todo"]})
        else:
            journal["denied"].append(f"{r['name']}: {added} z {len(r['todo'])}")
        time.sleep(1.2)
    PUSHED.write_text(json.dumps(pushed, indent=1, ensure_ascii=False))
    (HERE / "last_forward.json").write_text(json.dumps(journal, ensure_ascii=False, indent=1))


def cmd_redate(args: argparse.Namespace) -> None:
    """Přepíše dřív zapsané epizody (bez data) na den posledního dílu daného seriálu.

    Stremio zná jen datum posledního zhlédnutého dílu. Zápis s 'unknown' je sice
    pravdivý, ale nepočítá se do statistik, takže tenhle příkaz historii přepíše.
    """
    pushed = json.loads(PUSHED.read_text()) if PUSHED.exists() else {"movies": [], "episodes": []}
    eps = pushed.get("episodes") or []
    if not eps:
        sys.exit("Nic k přepisu – pushed.json je prázdný.")
    alias = load_alias()
    last_by_id = {}
    for i in fetch_raw():
        last_by_id[alias.get(i.get("_id"), i.get("_id"))] = (i.get("state") or {}).get("lastWatched")

    groups: dict[str, list[tuple[int, int]]] = {}
    for imdb, s, e in eps:
        if args.only and imdb != args.only:
            continue
        groups.setdefault(imdb, []).append((s, e))
    if not groups:
        sys.exit("Nic k přepisu pro zadaný filtr.")

    import track
    for imdb, pairs in sorted(groups.items()):
        when = args.date or last_by_id.get(imdb)
        if not when:
            print(f"  {imdb}: datum neznámé, přeskakuji")
            continue
        seasons = sorted({p[0] for p in pairs})
        print(f"  {imdb}: {len(pairs)} epizod → {when[:10]}")
        if not args.yes:
            continue
        remove_body = {"shows": [{"ids": {"imdb": imdb}, "seasons": [
            {"number": s, "episodes": [{"number": e} for ss, e in pairs if ss == s]}
            for s in seasons]}]}
        track._req("POST", "/sync/history/remove", body=remove_body)
        time.sleep(1.2)
        add_body = {"shows": [{"ids": {"imdb": imdb}, "seasons": [
            {"number": s, "episodes": [{"number": e, "watched_at": when} for ss, e in pairs if ss == s]}
            for s in seasons]}]}
        res = track._req("POST", "/sync/history", body=add_body)[0]
        print("    znovu zapsáno:", (res.get("added") or {}).get("episodes"))
        time.sleep(1.2)
        hist = track.paged(f"/sync/history/shows/{imdb}", {"limit": "200"})
        print(f"    kontrola: v historii Traktu teď {len(hist)} záznamů (čekáno {len(pairs)})")


def _norm_title(t: str, keep_parts: bool = False) -> str:
    """Název k porovnání: bez interpunkce, bez úvodního „The“.

    S `keep_parts` zůstává i značka „(1)/(2)“, která je potřeba tam, kde obě strany
    vedou hodinový díl jako dva díly — bez ní se oba půldíly slepí do jednoho.
    """
    t = (t or "").lower()
    if not keep_parts:
        t = re.sub(r"\s*\((?:1|2|part\s*\d)\)\s*$", "", t)
    else:
        t = re.sub(r"\((1|2|part\s*\d)\)", r" \1 ", t)
    t = re.sub(r"[^a-z0-9áčďéěíňóřšťúůýž ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip().removeprefix("the ")


def trakt_titles(imdb: str) -> dict[tuple[int, int], str]:
    """Názvy epizod podle Traktu: {(season, episode): title}. Kvůli mapování z Cinetemy."""
    cache_path = HERE / "trakt_titles_cache.json"
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    if imdb not in cache:
        import track
        try:
            seasons = track._req("GET", f"/shows/{imdb}/seasons",
                                 params={"extended": "episodes"})[0]
        except Exception as e:
            print(f"  ! seznam epizod pro {imdb} selhal: {e}", file=sys.stderr)
            return {}
        cache[imdb] = {f"{int(s['number'])}:{int(e['number'])}": e.get("title") or ""
                       for s in seasons or [] for e in s.get("episodes") or []}
        cache_path.write_text(json.dumps(cache, ensure_ascii=False))
    return {tuple(int(x) for x in k.split(":")): v for k, v in cache[imdb].items()}


def map_cinemeta_pairs(imdb: str, cm_pairs: set) -> dict:
    """Přeloží dvojice z číslování Cinemety do číslování Traktu.

    Cinemeta dělí hodinové díly na dva záznamy („Fun Run (1)“ a „(2)“), Trakt je vede
    jako jeden. Mapuje se podle názvu, v rámci jedné řady a pořadí (monotónně), takže
    se záměna mezi řadami nemůže stát. Co se namapovat nedá, se vrátí ve `unmatched`
    a do Traktu se neposílá.
    """
    videos = cinemeta_videos(imdb)
    titles = trakt_titles(imdb)
    by_season: dict[int, list[tuple[int, str]]] = {}
    for (s, e), t in titles.items():
        by_season.setdefault(s, []).append((e, t))
    for s in by_season:
        by_season[s].sort()

    mapped: set = set()
    unmatched: list = []
    parts: dict = {}
    for season in sorted({s for s, _ in cm_pairs}):
        candidates = by_season.get(season) or []
        marked = [v for v in videos if v[0] == season and (v[0], v[1]) in cm_pairs]
        if not candidates:
            unmatched += [(s, e, t) for s, e, t in marked]
            continue
        pointer = 0
        for _s, _e, title in marked:
            want = _norm_title(title)
            want_full = _norm_title(title, keep_parts=True)
            # 1) přesná shoda včetně značky (1)/(2) — rozhodne tam, kde ji má i Trakt
            exact = None
            for j in range(pointer, min(pointer + 4, len(candidates))):
                if _norm_title(candidates[j][1], keep_parts=True) == want_full:
                    exact = j
                    break
            if exact is not None:
                pointer = exact
                pair = (season, candidates[exact][0])
                mapped.add(pair)
                parts[pair] = parts.get(pair, 0) + 1
                continue
            # 2) bez značky — tam, kde Trakt hodinový díl vede jako jeden
            best, best_ratio = None, 0.0
            for j in range(pointer, min(pointer + 4, len(candidates))):
                ratio = difflib.SequenceMatcher(None, want, _norm_title(candidates[j][1])).ratio()
                if ratio > best_ratio:
                    best, best_ratio = j, ratio
            if best is None or best_ratio < 0.6:
                unmatched.append((season, _e, title))
                continue
            pointer = best
            pair = (season, candidates[best][0])
            mapped.add(pair)
            parts[pair] = parts.get(pair, 0) + 1
    return {"mapped": mapped, "unmatched": unmatched,
            "parts": {p: n for p, n in parts.items() if n > 1}}


def cmd_exact(args: argparse.Namespace) -> None:
    """Srovná Trakt přesně na seznam zhlédnutých dílů podle Stremia — přidá i odebere.

    `push` jen doplňuje a `forward` doplní mezeru k poslednímu dokoukanému dílu; oba
    nechají v Traktu i díly, které uživatel přeskočil. Tenhle příkaz udělá z Traktu
    zrcadlo Stremia: co Stremio jako zhlédnuté nevede, z Traktu zmizí.
    """
    import track
    alias = load_alias()
    items = fetch_raw()
    item = next((i for i in items
                 if i.get("_id") == args.only or alias.get(i.get("_id")) == args.only), None)
    if item is None:
        sys.exit(f"V knihovně Stremia není {args.only}.")
    st = stremio_state(item)
    trakt_id = alias.get(item.get("_id"), item.get("_id"))
    if st.get("error") or not st.get("verified"):
        sys.exit(f"{st['name']}: mapování není ověřené ({st.get('error') or st.get('reason')}), "
                 "použij `forward`.")
    if not st["watched"]:
        sys.exit(f"{st['name']}: Stremio nic zhlédnuté nehlásí.")

    res = map_cinemeta_pairs(trakt_id, st["watched"])
    target = {p for p in res["mapped"] if p[0] != 0 or args.include_specials}
    current = trakt_watched_episodes(trakt_id)
    add = sorted(target - current)
    remove = sorted(current - target)

    print(f"{st['name']} ({trakt_id})")
    print(f"  Stremio označuje {len(st['watched'])} dílů → v číslování Traktu {len(target)}")
    print(f"  Trakt má {len(current)} → shoda {len(target & current)}")
    if res["unmatched"]:
        print(f"  ⚠ namapovat nešlo {len(res['unmatched'])}: "
              + ", ".join(f"S{s}E{e} ({t})" for s, e, t in res["unmatched"][:5]))
    if res["parts"]:
        print(f"  (u {len(res['parts'])} dílů má Cinemeta dva půldíly, Trakt jeden — počítá se jako jeden)")
    print(f"  doplnit: {len(add)}" + (f" {add}" if len(add) <= 20 else ""))
    print(f"  odebrat: {len(remove)}" + (f" {remove}" if len(remove) <= 20 else ""))
    if not args.yes:
        print("\n(Dry-run. Pro skutečnou změnu přidej --yes.)")
        return

    journal = {"imdb": trakt_id, "name": st["name"], "add": add, "remove": remove,
               "target": len(target)}
    if remove:
        seasons = sorted({p[0] for p in remove})
        body = {"shows": [{"ids": {"imdb": trakt_id}, "seasons": [
            {"number": s, "episodes": [{"number": e} for ss, e in remove if ss == s]}
            for s in seasons]}]}
        track._req("POST", "/sync/history/remove", body=body)
        print(f"  odebráno {len(remove)} dílů")
        time.sleep(1.2)
    if add:
        when = args.date or "unknown"
        seasons = sorted({p[0] for p in add})
        body = {"shows": [{"ids": {"imdb": trakt_id}, "seasons": [
            {"number": s, "episodes": [{"number": e, "watched_at": when} for ss, e in add if ss == s]}
            for s in seasons]}]}
        res2 = track._req("POST", "/sync/history", body=body)[0]
        got = (res2.get("added") or {}).get("episodes") or 0
        print(f"  zapsáno {got} z {len(add)}")
        journal["added"] = got
        time.sleep(1.2)

    after = trakt_watched_episodes(trakt_id)
    print(f"  kontrola: Trakt teď má {len(after)}, cíl {len(target)} → "
          f"{'sedí' if after == target else 'NESEDÍ: ' + str(sorted(after ^ target)[:10])}")
    (HERE / "last_exact.json").write_text(json.dumps(journal, ensure_ascii=False, indent=1))


def main() -> None:
    ap = argparse.ArgumentParser(description="Most Stremio → Trakt")
    sub = ap.add_subparsers(dest="cmd", required=True)
    lg = sub.add_parser("_login")
    lg.add_argument("email")
    lg.set_defaults(func=cmd__login)
    sub.add_parser("list", help="co je ve Stremio knihovně").set_defaults(func=cmd_list)
    sub.add_parser("fetch", help="stáhne knihovnu ze Stremia").set_defaults(func=cmd_fetch)
    sub.add_parser("compare", help="rozdíl proti Traktu").set_defaults(func=cmd_compare)
    pu = sub.add_parser("push", help="doplnit chybějící do Traktu")
    pu.add_argument("--yes", action="store_true", help="skutečně zapsat")
    pu.add_argument("--include-specials", action="store_true", help="zapsat i speciály (S0)")
    pu.set_defaults(func=cmd_push)
    rd = sub.add_parser("redate", help="doplní datum u dřív zapsaných epizod")
    rd.add_argument("--yes", action="store_true", help="skutečně přepsat")
    rd.add_argument("--only", help="jen jeden titul (IMDb ID)")
    rd.add_argument("--date", help="vlastní datum (ISO); výchozí = poslední díl seriálu")
    rd.set_defaults(func=cmd_redate)
    fw = sub.add_parser("forward", help="doplní Trakt na poslední dokoukaný díl podle Stremia")
    fw.add_argument("--yes", action="store_true", help="skutečně zapsat")
    fw.add_argument("--only", help="jen jeden titul (IMDb ID)")
    fw.add_argument("--date", help="datum zápisu, nebo 'unknown'")
    fw.add_argument("--max", type=int, help="zapsat jen seriály s tolika nejvýše chybějícími díly")
    fw.set_defaults(func=cmd_forward)
    ex = sub.add_parser("exact", help="srovná Trakt přesně na zhlédnuté díly podle Stremia")
    ex.add_argument("--only", required=True, help="titul (IMDb ID ze Stremia nebo Traktu)")
    ex.add_argument("--yes", action="store_true", help="skutečně zapsat (i mazat)")
    ex.add_argument("--date", help="datum pro doplněné díly (výchozí: unknown)")
    ex.add_argument("--include-specials", action="store_true", help="počítat i speciály (S0)")
    ex.set_defaults(func=cmd_exact)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
