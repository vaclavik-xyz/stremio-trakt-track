#!/usr/bin/env python3
"""Most Stremio → Trakt.

Stremio si svou knihovnu a stav zhlédnutí drží ve vlastním cloudu (to je jeho
základní funkce a nepadá). Trakt propojení ve Stremiu ale odpadá po hodinách.
Tenhle most čte stav ze Stremia a doplňuje, co v Traktu chybí.

Použití:
  python3 stremio_bridge.py list        # co je ve stremio knihovně
  python3 stremio_bridge.py compare     # co má Stremio zhlédnuté a Trakt ne
  python3 stremio_bridge.py push        # zápis chybějícího do Traktu (dry-run)
  python3 stremio_bridge.py push --yes  # skutečně zapsat

Přihlášení (jednorázově): bash setup_stremio.sh
Ukládá se jen authKey do stremio.json (práva 600), heslo se nikam neukládá.

Zásada: o zápisu se rozhoduje jen z úplných dat. Když se čtení z Traktu nebo
Stremia nepovede, příkaz skončí chybou — nikdy nepokračuje s prázdnou množinou,
protože „Trakt nemá nic“ by znamenalo zapsat všechno znovu (Trakt duplicity
nevyřazuje, každý zápis je další zhlédnutí).
"""
from __future__ import annotations

import argparse
import base64
import difflib
import json
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request
import zlib

CODE_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(CODE_DIR))

import common  # noqa: E402
import writes  # noqa: E402

HERE = common.DATA_DIR
STREMIO_CFG = HERE / "stremio.json"
RAW = HERE / "stremio_library.json"
PUSHED = HERE / "pushed.json"
GAPS = HERE / "gaps.json"
CINEMETA_CACHE = HERE / "cinemeta_cache.json"
TRAKT_EPISODES_CACHE = HERE / "trakt_episodes_cache.json"
LAST_PUSH = HERE / "last_push.json"
LAST_FORWARD = HERE / "last_forward.json"
LAST_EXACT = HERE / "last_exact.json"
SAPI = "https://api.strem.io/api"
CINEMETA = "https://v3-cinemeta.strem.io/meta/series/{}.json"
UA = "stremio-trakt-bridge/2.0"


class BridgeError(RuntimeError):
    """Data from Stremio/Cinemeta could not be read or does not make sense."""


# ------------------------------------------------------------------ pomocné


def load_stremio() -> dict:
    if not STREMIO_CFG.exists():
        sys.exit("Chybí přihlášení ke Stremiu. Spusť: bash setup_stremio.sh")
    return json.loads(STREMIO_CFG.read_text())


def save_stremio(cfg: dict) -> None:
    common.atomic_write_json(STREMIO_CFG, cfg, indent=2)


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
    except (TimeoutError, OSError, ValueError) as e:
        sys.exit(f"Stremio API: {e}")
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


def _ttl() -> float:
    return float(common.settings()["cache_ttl_hours"])


# ------------------------------------------------------------- stremio data


def fetch_raw(force: bool = False, max_age: int = 300) -> list:
    if not force and RAW.exists() and time.time() - RAW.stat().st_mtime < max_age:
        return json.loads(RAW.read_text())
    cfg = load_stremio()
    items = s_call("/datastoreGet",
                   {"authKey": cfg["authKey"], "collection": "libraryItem", "all": True})
    if not isinstance(items, list):
        sys.exit("Stremio nevrátilo seznam knihovny.")
    common.atomic_write_json(RAW, items)
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


def cinemeta_videos(stremio_id: str, refresh: bool = False) -> list[list]:
    """Videa seriálu v pořadí Cinemety: [[video_id, season, episode, title], …].

    Chyba nebo prázdná odpověď vyhodí BridgeError — prázdný seznam by vypadal jako
    seriál bez dílů."""
    cache = common.JsonCache(CINEMETA_CACHE, _ttl())
    if not refresh:
        hit = cache.get(stremio_id)
        if hit:
            return [list(x) for x in hit]
    try:
        req = urllib.request.Request(CINEMETA.format(stremio_id), headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=25) as r:
            meta = (json.loads(r.read()) or {}).get("meta") or {}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        raise BridgeError(f"metadata z Cinemety pro {stremio_id} se nepovedla: {e}") from None
    videos = [[v.get("id") or "", v.get("season"), v.get("episode", v.get("number")),
               v.get("name") or v.get("title") or ""]
              for v in (meta.get("videos") or [])]
    if not videos:
        raise BridgeError(f"Cinemeta u {stremio_id} nevrátila žádná videa")
    cache.put(stremio_id, videos)
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
                    _vid, s, e, _t = videos[i]
                    if s is not None and e is not None:
                        out["watched"].add((int(s), int(e)))
                # Stremio sám říká, který díl byl poslední → musí sedět na nejvyšší index
                if idx:
                    _vid, s, e, _t = videos[max(idx)]
                    out["marker"] = (ls, le)
                    if s is None or e is None:
                        out["verified"] = False
                        out["reason"] = "poslední díl nemá v Cinemetě číslo řady/dílu"
                    elif (int(s), int(e)) != (ls, le) and (ls, le) != (0, 0):
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


# --------------------------------------------------------------- trakt čtení
# Všechny tyhle funkce při chybě výjimku propustí. Prázdná množina tu znamená
# „Trakt opravdu nic nemá“, nikdy „nepovedlo se to přečíst“.


_WATCHED_SHOWS: dict | None = None


def trakt_watched_shows(refresh: bool = False) -> dict[str, set]:
    """Všechny zhlédnuté díly všech seriálů jedním voláním `/sync/watched/shows`.

    Klíč je IMDb ID a `trakt:<id>`. Na rozdíl od `/shows/{id}/progress/watched` sem
    patří i skryté řady — jinak by se díly z nich doplňovaly každou noc znovu.
    """
    global _WATCHED_SHOWS
    if _WATCHED_SHOWS is None or refresh:
        import track
        rows = track._req("GET", "/sync/watched/shows")[0]
        if not isinstance(rows, list):
            raise track.TraktError("/sync/watched/shows nevrátil seznam")
        out: dict[str, set] = {}
        for r in rows:
            ids = (r.get("show") or {}).get("ids") or {}
            eps = {(int(s["number"]), int(e["number"]))
                   for s in r.get("seasons") or [] for e in s.get("episodes") or []}
            for key in (ids.get("imdb"), f"trakt:{ids['trakt']}" if ids.get("trakt") else None):
                if key:
                    out.setdefault(key, set()).update(eps)
        _WATCHED_SHOWS = out
    return _WATCHED_SHOWS


def trakt_watched_episodes(imdb: str, refresh: bool = False) -> set[tuple[int, int]]:
    return set(trakt_watched_shows(refresh).get(imdb, set()))


def trakt_watched_movies() -> set[str]:
    """Zhlédnuté filmy přímo z Traktu. Žádný fallback na lokální DB — ta je starší
    a film scrobblovaný dnes by se zapsal podruhé."""
    import track
    rows = track._req("GET", "/sync/watched/movies")[0]
    if not isinstance(rows, list):
        raise track.TraktError("/sync/watched/movies nevrátil seznam")
    return {((r.get("movie") or {}).get("ids") or {}).get("imdb") for r in rows} - {None}


def trakt_seasons(imdb: str, refresh: bool = False) -> dict[tuple[int, int], str]:
    """Díly, které Trakt u seriálu zná, s názvy: {(season, episode): title}."""
    cache = common.JsonCache(TRAKT_EPISODES_CACHE, _ttl())
    hit = None if refresh else cache.get(imdb)
    if not hit:
        import track
        seasons = track._req("GET", f"/shows/{imdb}/seasons", params={"extended": "episodes"})[0]
        if not isinstance(seasons, list):
            raise track.TraktError(f"/shows/{imdb}/seasons nevrátil seznam")
        hit = {f"{int(s['number'])}:{int(e['number'])}": e.get("title") or ""
               for s in seasons for e in s.get("episodes") or []}
        cache.put(imdb, hit)
    return {tuple(int(x) for x in k.split(":")): v for k, v in hit.items()}


def trakt_episode_inventory(imdb: str, refresh: bool = False) -> set[tuple[int, int]]:
    return set(trakt_seasons(imdb, refresh))


def load_alias() -> dict:
    """Některé tituly má Trakt pod jiným IMDb ID než Stremio → alias.json."""
    p = HERE / "alias.json"
    if not p.exists():
        return {}
    data = json.loads(p.read_text())
    return {k: v for k, v in data.items() if not k.startswith("_")}


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
        try:
            st = stremio_state(it)
        except (BridgeError, ValueError) as e:
            print(f"  {it.get('name')} — ⚠ {e}")
            continue
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
        imdb = it.get("_id") or ""
        try:
            st = stremio_state(it)
        except (BridgeError, ValueError) as e:
            gaps["unknown"].append({"name": it.get("name"), "imdb": imdb, "reason": str(e)})
            continue
        if st.get("error"):
            gaps["unknown"].append({"name": st["name"], "imdb": imdb, "reason": st["error"]})
            continue
        if not st["watched"]:
            continue
        trakt_id = alias.get(imdb, imdb)
        if st["kind"] == "series":
            if not st.get("verified"):
                gaps["unknown"].append({"name": st["name"], "imdb": imdb,
                                        "reason": st.get("reason") or "neověřeno"})
                continue
            inventory = trakt_episode_inventory(trakt_id)
            bogus = sorted(p for p in st["watched"] if p not in inventory)
            if bogus:
                gaps["unknown"].append({
                    "name": st["name"], "imdb": imdb,
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
    common.atomic_write_json(GAPS, gaps, indent=2)
    print("Uloženo do gaps.json.")


# ----------------------------------------------------------------- zápisy


def _record_pushed(pairs_by_show: dict[str, list]) -> None:
    """pushed.json: co most zapsal (pro `redate`). Bez duplicit."""
    pushed = common.read_json(PUSHED, {"movies": [], "episodes": []})
    have = {tuple(x) for x in pushed.get("episodes") or []}
    for imdb, pairs in pairs_by_show.items():
        for a, b in pairs:
            have.add((imdb, int(a), int(b)))
    pushed["episodes"] = sorted([list(x) for x in have])
    common.atomic_write_json(PUSHED, pushed)


def _journal_update(journal: common.Journal, result: dict) -> None:
    """Přepíše deník podle výsledku zápisu (volá se po každém requestu)."""
    shows: dict[str, dict] = {}
    for i in result["added"]:
        if i["kind"] == "episode":
            row = shows.setdefault(i["ids"].get("imdb") or str(i["ids"]),
                                   {"imdb": i["ids"].get("imdb"), "name": i.get("name"), "pairs": []})
            row["pairs"].append([i["season"], i["episode"]])
    journal["movies"] = [{"imdb": i["ids"].get("imdb"), "name": i.get("name")}
                         for i in result["added"] if i["kind"] == "movie"]
    journal["shows" if "shows" in journal.data else "written"] = list(shows.values())
    journal["denied"] = list(result["denied"])
    journal["repeat"] = [{"name": i.get("name"), "key": writes.item_key(i)} for i in result["repeat"]]
    journal.save()


def _report_write(result: dict) -> None:
    if result["repeat"]:
        print(f"  ! {len(result['repeat'])} položek přeskočeno — stejný zápis proběhl v posledních "
              f"{common.settings()['repeat_guard_days']} dnech (anomálie, Trakt by počítal další "
              "zhlédnutí; prověř, proč je Trakt nevidí; --force-repeat zapíše i tak)")
        for i in result["repeat"][:10]:
            print(f"     {writes.item_key(i)}")
    for d in result["denied"]:
        print(f"  ! {d}")


def cmd_push(args: argparse.Namespace) -> None:
    if not GAPS.exists():
        sys.exit("Nejdřív spusť: python3 stremio_bridge.py compare")
    gaps = json.loads(GAPS.read_text())
    # Nefiltrujeme podle pushed.json: o tom, co chybí, rozhoduje živý stav Traktu
    # (gaps.json). Opakovanému zápisu téhož brání pojistka ve writes.py.
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
        print(f"  (vynecháno {skipped_specials} speciálů S0 – přidej --include-specials, pokud je chceš)")
    for m in movies:
        print(f"  film: {m['name']} ({m['last'][:10] if m.get('last') else 'unknown'})")
    for s in shows:
        print(f"  seriál: {s['name']} — {len(s['missing'])} epizod")

    if not args.yes:
        print("\n(Dry-run. Pro skutečný zápis přidej --yes.)")
        return

    items = [writes.movie_item({"imdb": m["imdb"]}, m.get("last"), m["name"]) for m in movies]
    for s in shows:
        last_ep = tuple(s.get("last_ep") or ())
        for a, b in s["missing"]:
            # přesné datum zná Stremio jen u posledního dílu; starší jsou "unknown"
            when = s["last"] if (a, b) == last_ep else None
            items.append(writes.episode_item({"imdb": s["imdb"]}, a, b, when, s["name"]))

    journal = common.Journal(LAST_PUSH, {"movies": [], "shows": [], "denied": [], "repeat": []})
    result = writes.add_history(items, cmd="push", force_repeat=args.force_repeat,
                                on_progress=lambda r: _journal_update(journal, r))
    _journal_update(journal, result)
    _record_pushed({row["imdb"]: row["pairs"] for row in journal["shows"]})
    print(f"  zapsáno: {sum(1 for i in result['added'] if i['kind'] == 'movie')} filmů, "
          f"{sum(1 for i in result['added'] if i['kind'] == 'episode')} epizod")
    _report_write(result)


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
        if (se, ep) not in inv:
            skipped.append(f"{it.get('name')}: Stremio uvádí poslední dokoukaný S{se}E{ep}, "
                           f"takový díl Trakt u {imdb} nezná")
            continue
        done = trakt_watched_episodes(imdb)
        mx = max(done) if done else None
        start = inv.index(mx) + 1 if mx in inv else 0
        todo = [e for e in inv[start:inv.index((se, ep)) + 1]
                if e not in done and (e[0] != 0 or args.include_specials)]
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

    selected = [r for r in rows if args.only is None or r["imdb"] == args.only]
    common.atomic_write_json(HERE / "forward.json", selected, indent=2)
    print(f"\nCelkem k doplnění: {sum(len(r['todo']) for r in selected)} dílů.")
    if not args.yes:
        print("(Dry-run. Pro zápis přidej --yes. Upsat datum: --date unknown, nebo výchozí = den posledního dílu.)")
        return

    to_write = [r for r in selected if args.max is None or len(r["todo"]) <= args.max]
    journal = common.Journal(LAST_FORWARD, {
        "written": [], "denied": [], "repeat": [], "skipped": skipped,
        "over_max": [{"imdb": r["imdb"], "name": r["name"], "todo": r["todo"],
                      "marker": r["marker"]} for r in selected if r not in to_write]})
    for r in selected:
        if r not in to_write:
            print(f"  {r['name']}: {len(r['todo'])} dílů – přes limit --max={args.max}, "
                  f"nechávám ke schválení (zapiš ručně s --only {r['imdb']})")
    items = []
    for r in to_write:
        when = args.date or r["last"]
        when = None if when == "unknown" else when
        items += [writes.episode_item({"imdb": r["imdb"]}, a, b, when, r["name"]) for a, b in r["todo"]]
    result = writes.add_history(items, cmd="forward", force_repeat=args.force_repeat,
                                on_progress=lambda res: _journal_update(journal, res))
    _journal_update(journal, result)
    _record_pushed({row["imdb"]: row["pairs"] for row in journal["written"]})
    for row in journal["written"]:
        print(f"  {row['name']}: zapsáno {len(row['pairs'])}")
    _report_write(result)


def cmd_redate(args: argparse.Namespace) -> None:
    """Přepíše dřív zapsané epizody (bez data) na den posledního dílu daného seriálu.

    Stremio zná jen datum posledního zhlédnutého dílu. Zápis s 'unknown' je sice
    pravdivý, ale nepočítá se do statistik, takže tenhle příkaz historii přepíše.
    """
    import track
    pushed = common.read_json(PUSHED, {"movies": [], "episodes": []})
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


# ---------------------------------------------------------- párování dílů


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


PART_MARK = re.compile(r"\((?:\d|part\s*\d)\)\s*$|\bpart\s*\d+\b")


def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def align_season(cm: list[tuple[int, str]], tr: list[tuple[int, str]],
                 threshold: float = 0.6) -> list[int | None]:
    """Zarovná všechny díly jedné řady Cinemety na díly Traktu.

    `cm` a `tr` jsou [(číslo dílu, název)] v pořadí. Vrátí pro každý prvek `cm` číslo
    dílu v Traktu, nebo None. Dvojice se drží pořadí (monotónně); dva sousední díly
    Cinemety smí padnout na jeden díl Traktu (hodinový díl rozdělený na „(1)/(2)“).

    1) Stejný počet dílů a názvy na stejných pozicích většinou sedí (nebo se nedají
       porovnat, např. samé „Episode N“/„TBA“) → rozhoduje číslo dílu.
    2) Jinak globální zarovnání (DP) přes celé sekvence: skóre je podobnost názvů,
       shoda čísla dílu je jen malý bonus pro remízy.
    """
    n, m = len(cm), len(tr)
    if not n or not m:
        return [None] * n
    full_a = [_norm_title(t, keep_parts=True) for _, t in cm]
    strip_a = [_norm_title(t) for _, t in cm]
    full_b = [_norm_title(t, keep_parts=True) for _, t in tr]
    strip_b = [_norm_title(t) for _, t in tr]
    parted = [bool(PART_MARK.search((t or "").lower())) for _, t in cm]

    def sim(i: int, j: int) -> float:
        if not strip_a[i] or not strip_b[j]:
            base = threshold        # bez názvu nevíme nic; rozhodne pořadí a číslo
        else:
            base = max(_ratio(full_a[i], full_b[j]), _ratio(strip_a[i], strip_b[j]) - 0.02)
        return base + (0.05 if cm[i][0] == tr[j][0] else 0.0)

    if n == m and [e for e, _ in cm] == [e for e, _ in tr]:
        comparable = [i for i in range(n) if strip_a[i] and strip_b[i]]
        agree = sum(1 for i in comparable if sim(i, i) >= threshold + 0.05)
        if not comparable or agree * 2 >= len(comparable):
            return [e for e, _ in tr]

    neg = float("-inf")
    # A[i][j]: nejlepší skóre pro prvních i dílů cm, kde díl i-1 padl na tr j-1
    # B[i][j]: nejlepší skóre pro prvních i dílů cm při použití tr[0:j]
    A = [[neg] * (m + 1) for _ in range(n + 1)]
    B = [[0.0] * (m + 1) for _ in range(n + 1)]
    how_a: dict[tuple[int, int], str] = {}
    how_b: dict[tuple[int, int], str] = {}
    for i in range(1, n + 1):
        how_b[(i, 0)] = "skip_cm"
        for j in range(1, m + 1):
            s = sim(i - 1, j - 1)
            if s >= threshold:
                best, how = B[i - 1][j - 1] + s, "new"
                # druhá polovina téhož dílu Traktu: jen když má díl Cinemety značku
                # „(1)/(2)“ a bez ní název sedí — „Episode 12“ na „Episode 11“ nesmí
                if (A[i - 1][j] > neg and parted[i - 1] and strip_b[j - 1]
                        and _ratio(strip_a[i - 1], strip_b[j - 1]) >= 0.9):
                    rep = A[i - 1][j] + s - 0.1
                    if rep > best:
                        best, how = rep, "repeat"
                A[i][j], how_a[(i, j)] = best, how
            cands = [(A[i][j], "match"), (B[i][j - 1], "skip_tr"), (B[i - 1][j], "skip_cm")]
            B[i][j], how_b[(i, j)] = max(cands, key=lambda c: c[0])

    out: list[int | None] = [None] * n
    i, j, in_a = n, m, False
    while i > 0:
        if in_a:
            out[i - 1] = tr[j - 1][0]
            if how_a[(i, j)] == "repeat":
                i -= 1
            else:
                i, j, in_a = i - 1, j - 1, False
            continue
        step = how_b[(i, j)] if j > 0 else "skip_cm"
        if step == "skip_tr":
            j -= 1
        elif step == "skip_cm":
            i -= 1
        else:
            in_a = True
    return out


def map_pairs(videos: list, titles: dict, cm_pairs: set, threshold: float = 0.6) -> dict:
    """Přeloží zhlédnuté dvojice z číslování Cinemety do číslování Traktu.

    `videos` jsou videa Cinemety ([id, season, episode, title]), `titles` díly Traktu
    {(s, e): title}. Zarovnávají se celé řady (i nezhlédnuté díly), ne jen zhlédnuté —
    jen tak se dá spolehlivě přeskočit mezera v sledování.
    """
    tr_by_season: dict[int, list[tuple[int, str]]] = {}
    for (s, e), t in titles.items():
        tr_by_season.setdefault(s, []).append((e, t))
    mapped: set = set()
    unmatched: list = []
    parts: dict = {}
    for season in sorted({s for s, _ in cm_pairs}):
        cm = [(int(v[2]), v[3]) for v in videos
              if v[1] is not None and v[2] is not None and int(v[1]) == season]
        tr = sorted(tr_by_season.get(season) or [])
        result = align_season(cm, tr, threshold)
        for (e, title), te in zip(cm, result):
            if (season, e) not in cm_pairs:
                continue
            if te is None:
                unmatched.append((season, e, title))
            else:
                mapped.add((season, te))
                parts[(season, te)] = parts.get((season, te), 0) + 1
        known = {e for e, _ in cm}
        unmatched += [(season, e, "") for s, e in sorted(cm_pairs) if s == season and e not in known]
    return {"mapped": mapped, "unmatched": unmatched,
            "parts": {p: k for p, k in parts.items() if k > 1}}


def map_cinemeta_pairs(stremio_id: str, trakt_id: str, cm_pairs: set) -> dict:
    """Cinemeta se ptá pod Stremio ID (z něj vznikla bitová mapa), Trakt pod svým."""
    return map_pairs(cinemeta_videos(stremio_id), trakt_seasons(trakt_id), cm_pairs,
                     float(common.settings()["match_threshold"]))


def cmd_exact(args: argparse.Namespace) -> None:
    """Srovná Trakt přesně na seznam zhlédnutých dílů podle Stremia — přidá i odebere.

    `push` jen doplňuje a `forward` doplní mezeru k poslednímu dokoukanému dílu; oba
    nechají v Traktu i díly, které uživatel přeskočil. Tenhle příkaz udělá z Traktu
    zrcadlo Stremia: co Stremio jako zhlédnuté nevede, z Traktu zmizí.

    Pojistky: když se něco nenamapuje nebo je cíl prázdný, nic se nemění. Nejdřív se
    doplňuje, pak maže; mazané záznamy (s history id a původním datem) jdou do deníku
    a do write_log.jsonl ještě před smazáním.
    """
    alias = load_alias()
    items = fetch_raw()
    item = next((i for i in items
                 if i.get("_id") == args.only or alias.get(i.get("_id")) == args.only), None)
    if item is None:
        sys.exit(f"V knihovně Stremia není {args.only}.")
    stremio_id = item.get("_id")
    st = stremio_state(item)
    trakt_id = alias.get(stremio_id, stremio_id)
    if st.get("error") or not st.get("verified"):
        sys.exit(f"{st['name']}: mapování není ověřené ({st.get('error') or st.get('reason')}), "
                 "použij `forward`.")
    if not st["watched"]:
        sys.exit(f"{st['name']}: Stremio nic zhlédnuté nehlásí.")

    res = map_cinemeta_pairs(stremio_id, trakt_id, st["watched"])
    target = {p for p in res["mapped"] if p[0] != 0 or args.include_specials}
    current = trakt_watched_episodes(trakt_id, refresh=True)
    add = sorted(target - current)
    remove = sorted(p for p in current - target if p[0] != 0 or args.include_specials)

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
    if not target:
        print("\nCíl je prázdný — nic neměním (to by smazalo celý seriál).", file=sys.stderr)
        sys.exit(common.EXIT_REFUSED)
    if res["unmatched"] and not args.allow_unmatched:
        print("\nNěco se nenamapovalo — nic neměním. Nenamapovaný díl by z Traktu zmizel, "
              "i když ho Stremio vede jako zhlédnutý. (--allow-unmatched to dovolí.)",
              file=sys.stderr)
        sys.exit(common.EXIT_REFUSED)
    if not args.yes:
        print("\n(Dry-run. Pro skutečnou změnu přidej --yes.)")
        return

    journal = common.Journal(LAST_EXACT, {
        "imdb": trakt_id, "name": st["name"], "target": len(target),
        "add": add, "remove": remove, "remove_entries": [], "added": [], "removed": [],
        "denied": [], "repeat": [], "status": "started"})
    if remove:
        history = writes.fetch_show_history(trakt_id)
        remove_set = set(remove)
        journal["remove_entries"] = [h for h in history
                                     if (h["season"], h["episode"]) in remove_set]
        journal.save()          # co se bude mazat, je na disku dřív, než se to smaže

    if add:
        when = None if args.date in (None, "unknown") else args.date
        items_add = [writes.episode_item({"imdb": trakt_id}, s, e, when, st["name"]) for s, e in add]
        result = writes.add_history(items_add, cmd="exact", force_repeat=args.force_repeat)
        journal["added"] = [[i["season"], i["episode"]] for i in result["added"]]
        journal["denied"] = result["denied"]
        journal["repeat"] = [writes.item_key(i) for i in result["repeat"]]
        journal.save()
        print(f"  zapsáno {len(result['added'])} z {len(add)}")
        _report_write(result)
        if len(result["added"]) != len(add):
            journal["status"] = "stopped: add incomplete, nothing removed"
            journal.save()
            print("  ! doplnění neprošlo celé — nic nemažu", file=sys.stderr)
            sys.exit(common.EXIT_ERROR)

    if journal["remove_entries"]:
        rres = writes.remove_history(journal["remove_entries"], cmd="exact")
        journal["removed"] = rres["removed"]
        journal.save()
        print(f"  odebráno {len(rres['removed'])} záznamů historie ({len(remove)} dílů)"
              + (f", nenalezeno {len(rres['not_found'])}" if rres["not_found"] else ""))
        _drop_pushed(trakt_id, remove)

    after = trakt_watched_episodes(trakt_id, refresh=True)
    after_cmp = {p for p in after if p[0] != 0 or args.include_specials}
    ok = after_cmp == target
    journal["status"] = "done" if ok else "done: mismatch"
    journal.save()
    print(f"  kontrola: Trakt teď má {len(after_cmp)}, cíl {len(target)} → "
          f"{'sedí' if ok else 'NESEDÍ: ' + str(sorted(after_cmp ^ target)[:10])}")


def _drop_pushed(imdb: str, pairs) -> None:
    """Co `exact` z Traktu odebral, nesmí `redate` později vrátit."""
    pushed = common.read_json(PUSHED, None)
    if not pushed:
        return
    drop = {(imdb, int(s), int(e)) for s, e in pairs}
    pushed["episodes"] = [x for x in pushed.get("episodes") or [] if tuple(x) not in drop]
    common.atomic_write_json(PUSHED, pushed)


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
    pu.add_argument("--force-repeat", action="store_true",
                    help="zapsat i to, co se zapsalo v posledních dnech")
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
    fw.add_argument("--include-specials", action="store_true", help="doplnit i speciály (S0)")
    fw.add_argument("--force-repeat", action="store_true",
                    help="zapsat i to, co se zapsalo v posledních dnech")
    fw.set_defaults(func=cmd_forward)
    ex = sub.add_parser("exact", help="srovná Trakt přesně na zhlédnuté díly podle Stremia")
    ex.add_argument("--only", required=True, help="titul (IMDb ID ze Stremia nebo Traktu)")
    ex.add_argument("--yes", action="store_true", help="skutečně zapsat (i mazat)")
    ex.add_argument("--date", help="datum pro doplněné díly (výchozí: unknown)")
    ex.add_argument("--include-specials", action="store_true", help="počítat i speciály (S0)")
    ex.add_argument("--allow-unmatched", action="store_true",
                    help="pokračovat, i když se některé díly nenamapovaly")
    ex.add_argument("--force-repeat", action="store_true",
                    help="zapsat i to, co se zapsalo v posledních dnech")
    ex.set_defaults(func=cmd_exact)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
