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


# Chyby v datech jedné položky knihovny. TraktError mezi nimi záměrně není.
ITEM_ERRORS = (BridgeError, ValueError, KeyError, TypeError, IndexError, AttributeError)


# ------------------------------------------------------------------ pomocné


def load_stremio() -> dict:
    if not STREMIO_CFG.exists():
        print("Chybí přihlášení ke Stremiu.", file=sys.stderr)
        print(STREMIO_FIX, file=sys.stderr)
        sys.exit(common.EXIT_AUTH)
    return json.loads(STREMIO_CFG.read_text())


def save_stremio(cfg: dict) -> None:
    common.atomic_write_json(STREMIO_CFG, cfg, indent=2)


STREMIO_FIX = ("Náprava: bash setup_stremio.sh (potřebuje heslo ke Stremiu). Do té doby most "
               "nic nedoplňuje.")


def _http_json(req: urllib.request.Request, timeout: int, label: str):
    """GET/POST s opakováním u přechodných chyb (5xx, timeout, síť). Volá se jen
    pro čtení — Stremio i Cinemeta most nikdy nemění. Vyčerpané pokusy → BridgeError."""
    delays = common.retry_delays()
    last = ""
    for attempt in range(len(delays) + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code < 500 and e.code != 429:
                raise BridgeError(f"{label} HTTP {e.code}: "
                                  f"{e.read()[:200].decode(errors='replace')}") from None
            last = f"HTTP {e.code}"
        except urllib.error.URLError as e:
            last = f"síťová chyba: {e.reason}"
        except (TimeoutError, OSError) as e:
            last = f"síťová chyba: {e}"
        except ValueError as e:
            raise BridgeError(f"{label}: neplatná odpověď ({e})") from None
        if attempt < len(delays):
            common.sleep(delays[attempt])
    raise BridgeError(f"{label}: {last} (ani po {len(delays) + 1} pokusech)")


def s_call(path: str, payload: dict) -> object:
    req = urllib.request.Request(
        SAPI + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": UA}, method="POST")
    try:
        data = _http_json(req, 30, "Stremio API")
    except BridgeError as e:
        sys.exit(str(e))
    if isinstance(data, dict) and data.get("error"):
        err = data["error"]
        msg = (err.get("message") if isinstance(err, dict) else str(err)) or "?"
        if "session" in msg.lower() or "auth" in msg.lower():
            print(f"Stremio přihlášení neplatí ({msg}).", file=sys.stderr)
            print(STREMIO_FIX, file=sys.stderr)
            sys.exit(common.EXIT_AUTH)
        sys.exit(f"Stremio API: {msg}")
    return data.get("result") if isinstance(data, dict) else data


def cmd__login(args: argparse.Namespace) -> None:
    """Interní – volá ho setup_stremio.sh. Heslo jde po stdin, nikdy do logu."""
    common.require_interactive("Přihlášení ke Stremiu", "bash setup_stremio.sh v terminálu")
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


def decode_watched(text: str) -> dict:
    """Rozbalí bitovou mapu Stremia `state.watched`.

    Formát (stremio-core, `WatchedBitField::serialize`):
    `<anchor_video_id>:<anchor_length>:<base64(zlib(bitset))>`, kde
    - bit i (LSB-first: bajt i // 8, bit i % 8) = video i v seznamu videí seriálu,
    - `anchor_video_id` je video s NEJVYŠŠÍM nastaveným bitem (poslední dokoukaný
      díl, u Cinemety `tt…:season:episode`) a `anchor_length` = jeho index + 1.

    Vrátí {"anchor", "sid", "season", "episode", "n", "bits"}; `season`/`episode`
    jsou None, když ID kotvy nemá tvar `id:řada:díl`. Nečitelná mapa = ValueError
    (nikdy tiché „nic zhlédnuto“).
    """
    parts = (text or "").split(":")
    if len(parts) < 3:
        raise ValueError(f"bitová mapa má neznámý tvar: {text[:40]!r}")
    try:
        n = int(parts[-2])
        raw = zlib.decompress(base64.b64decode(parts[-1]))
    except (ValueError, zlib.error) as e:
        raise ValueError(f"bitová mapa se nedá rozbalit: {e}") from None
    anchor = ":".join(parts[:-2])
    bits = [i for i in range(len(raw) * 8) if raw[i // 8] >> (i % 8) & 1]
    m = re.fullmatch(r"(.+):(\d+):(\d+)", anchor)
    sid, season, episode = (m.group(1), int(m.group(2)), int(m.group(3))) if m else (anchor, None, None)
    return {"anchor": anchor, "sid": sid, "season": season, "episode": episode, "n": n, "bits": bits}


def realign(bits: list[int], n: int, anchor: str, video_ids: list[str]) -> list[int] | None:
    """Posune bity na aktuální seznam videí stejně jako Stremio
    (`WatchedBitField::construct_and_resize`).

    Když se od uložení mapy změnil seznam videí (přibyl speciál, Cinemeta vložila
    díl), kotva je jinde: offset = n - nový_index - 1 a starý bit k je teď k - offset.
    Kotva v seznamu není → None (Stremio by mapu zahodil celou).
    """
    try:
        new_idx = video_ids.index(anchor)
    except ValueError:
        return None
    offset = n - new_idx - 1
    return sorted(k - offset for k in bits if 0 <= k - offset < len(video_ids))


def cinemeta_videos(stremio_id: str, refresh: bool = False) -> list[list]:
    """Videa seriálu v pořadí Cinemety: [[video_id, season, episode, title], …].

    Chyba nebo prázdná odpověď vyhodí BridgeError — prázdný seznam by vypadal jako
    seriál bez dílů."""
    cache = common.JsonCache(CINEMETA_CACHE, _ttl())
    if not refresh:
        hit = cache.get(stremio_id)
        if hit:
            return [list(x) for x in hit]
    req = urllib.request.Request(CINEMETA.format(stremio_id), headers={"User-Agent": UA})
    meta = (_http_json(req, 25, f"Cinemeta {stremio_id}") or {}).get("meta") or {}
    videos = []
    for v in meta.get("videos") or []:
        s, e = v.get("season"), v.get("episode", v.get("number"))
        vid = v.get("id") or (f"{stremio_id}:{s}:{e}" if s is not None and e is not None else "")
        videos.append([vid, s, e, v.get("name") or v.get("title") or ""])
    if not videos:
        raise BridgeError(f"Cinemeta u {stremio_id} nevrátila žádná videa")
    cache.put(stremio_id, videos)
    return videos


def is_movie(item: dict) -> bool:
    """Jen skutečné filmy s IMDb ID — kanály, YouTube a ID z jiných addonů Trakt nezná."""
    return item.get("type") == "movie" and str(item.get("_id") or "").startswith("tt")


def stremio_state(item: dict) -> dict:
    """Vrátí stav zhlédnutí. U seriálů zarovná bitovou mapu na aktuální videa.

    Chyby dat (nečitelná mapa, Cinemeta nedostupná) vyhodí výjimku; volající je
    převede na „neověřeno“ u téhle jedné položky.
    """
    state = item.get("state") or {}
    kind = item.get("type")
    last = state.get("lastWatched")
    plays = int(state.get("timesWatched") or 0)
    out = {"watched": set(), "last": last, "plays": plays, "kind": kind,
           "name": item.get("name"), "verified": True}
    if kind == "series":
        w = state.get("watched")
        if w:
            wb = decode_watched(w)
            out["marker"] = (wb["season"], wb["episode"])
            stremio_id = item.get("_id") or wb["sid"]
            # Bity za kotvou (index >= anchor_length) by podle formátu neměly existovat,
            # ale v živých datech se objevují (i zjevně nesmyslné: kotva S0E1, bit 199).
            # Za zhlédnuté se nepočítají; jen se hlásí v `beyond_anchor`.
            inside = [k for k in wb["bits"] if k < wb["n"]]
            beyond = [k for k in wb["bits"] if k >= wb["n"]]
            videos = cinemeta_videos(stremio_id)
            idx = realign(inside, wb["n"], wb["anchor"], [v[0] for v in videos])
            if idx is None:     # seznam v cache může být starý
                videos = cinemeta_videos(stremio_id, refresh=True)
                idx = realign(inside, wb["n"], wb["anchor"], [v[0] for v in videos])
            if idx is None:
                out["verified"] = False
                out["error"] = f"poslední dokoukaný díl {wb['anchor']} v Cinemetě není"
            else:
                shift = wb["n"] - 1 - [v[0] for v in videos].index(wb["anchor"])
                if shift:
                    out["realigned"] = shift
                for i in idx:
                    _vid, s, e, _t = videos[i]
                    if s is not None and e is not None:
                        out["watched"].add((int(s), int(e)))
                if beyond:
                    extra = realign(beyond, wb["n"], wb["anchor"], [v[0] for v in videos]) or []
                    out["beyond_anchor"] = sorted({(int(videos[i][1]), int(videos[i][2]))
                                                   for i in extra
                                                   if videos[i][1] is not None
                                                   and videos[i][2] is not None})
        vid = state.get("video_id")
        if vid and ":" in vid:
            parts = vid.split(":")
            if len(parts) >= 3:
                try:
                    out["last_ep"] = (int(parts[-2]), int(parts[-1]))
                except ValueError:
                    pass
    elif is_movie(item):
        # `lastWatched` se nastaví už při puštění; zhlédnuto = Stremio započítal
        # přehrání (timesWatched, ~70 % délky) nebo ho uživatel ručně označil
        if plays > 0 or state.get("flaggedWatched"):
            out["watched"].add((0, 0))
    return out


# --------------------------------------------------------------- trakt čtení
# Všechny tyhle funkce při chybě výjimku propustí. Prázdná množina tu znamená
# „Trakt opravdu nic nemá“, nikdy „nepovedlo se to přečíst“.


_WATCHED: dict[str, set] = {}


def _shape_error(what: str) -> Exception:
    import track
    return track.TraktError(f"{what} — odpověď Traktu nemá očekávaný tvar, nezapisuju nic")


def parse_progress(imdb: str, pr) -> set[tuple[int, int]]:
    """Zhlédnuté díly z `/shows/{id}/progress/watched`.

    Pojistka proti tiché ztrátě: když Trakt hlásí `completed > 0`, ale v odpovědi
    nejde najít ani jeden zhlédnutý díl (chybí `seasons`, jiný tvar), je to chyba —
    prázdná množina by znamenala zapsat celý seriál znovu.
    """
    if not isinstance(pr, dict):
        raise _shape_error(f"/shows/{imdb}/progress/watched nevrátil objekt")
    done = set()
    for season in pr.get("seasons") or []:
        for ep in season.get("episodes") or []:
            if ep.get("completed"):
                done.add((int(season["number"]), int(ep["number"])))
    try:
        completed = int(pr.get("completed") or 0)
    except (TypeError, ValueError):
        completed = 0
    if completed > 0 and not done:
        raise _shape_error(f"/shows/{imdb}/progress/watched: completed={completed}, ale žádný díl")
    return done


def trakt_watched_episodes(imdb: str, refresh: bool = False) -> set[tuple[int, int]]:
    """Zhlédnuté díly seriálu z `/shows/{id}/progress/watched` (hidden=false,
    specials=true). Chyba se propaguje — nikdy nevrací prázdno místo chyby.

    Pozor: bulk `/sync/watched/shows` tuhle informaci u některých účtů nenese
    (vrací jen `plays`/`last_watched_at`, bez `seasons`) — proto se z něj
    zhlédnuté díly neodvozují. Díly ve skrytých řadách tu chybí; opakovanému
    zápisu z toho důvodu brání pojistka ve writes.py.
    """
    if refresh or imdb not in _WATCHED:
        import track
        pr = track._req("GET", f"/shows/{imdb}/progress/watched",
                        params={"hidden": "false", "specials": "true"})[0]
        _WATCHED[imdb] = parse_progress(imdb, pr)
    return set(_WATCHED[imdb])


def trakt_watched_movies() -> set[str]:
    """Zhlédnuté filmy přímo z Traktu (celá historie filmů). Žádný fallback na
    lokální DB — ta je starší a film scrobblovaný dnes by se zapsal podruhé."""
    import track
    rows = track.paged("/sync/history/movies")
    ids = {((r.get("movie") or {}).get("ids") or {}).get("imdb") for r in rows if isinstance(r, dict)}
    ids.discard(None)
    if rows and not ids:
        raise _shape_error(f"/sync/history/movies: {len(rows)} záznamů, ale žádné IMDb ID")
    return ids


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
        if seasons and not hit:
            raise _shape_error(f"/shows/{imdb}/seasons: {len(seasons)} řad, ale žádný díl")
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
        if it.get("type") != "series" and not is_movie(it):
            continue
        try:
            st = stremio_state(it)
        except ITEM_ERRORS as e:
            # chyba v datech jedné položky nesmí zastavit ostatní; chyba Traktu
            # (TraktError) ale propadne dál — bez úplných dat se nezapisuje
            gaps["unknown"].append({"name": it.get("name"), "imdb": imdb,
                                    "reason": f"{type(e).__name__}: {e}"})
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
            if any(p not in inventory for p in st["watched"]):
                inventory = trakt_episode_inventory(trakt_id, refresh=True)
            bogus = sorted(p for p in st["watched"] if p not in inventory)
            if bogus:
                gaps["unknown"].append({
                    "name": st["name"], "imdb": imdb,
                    "reason": f"mapování dává neexistující epizody ({len(bogus)}), např. "
                              + ", ".join(f"S{a}E{b}" for a, b in bogus[:3])})
                continue
            done = trakt_watched_episodes(trakt_id)
            missing = sorted(st["watched"] - done)
            beyond = [p for p in st.get("beyond_anchor") or [] if p not in done]
            if beyond:
                gaps.setdefault("beyond_anchor", []).append(
                    {"imdb": trakt_id, "name": st["name"], "pairs": beyond})
            if missing:
                gaps["shows"].append({
                    "imdb": trakt_id, "name": st["name"], "last": st["last"],
                    "last_ep": st.get("last_ep"),
                    "missing": missing, "trakt_has": len(done),
                    "stremio_has": len(st["watched"]),
                })
        else:
            if trakt_id not in trakt_movies:
                gaps["movies"].append({"imdb": trakt_id, "name": st["name"], "last": st["last"]})
    return gaps


def cmd_fetch(_args: argparse.Namespace) -> None:
    items = fetch_raw(force=True)
    print(f"Staženo {len(items)} položek z Stremio knihovny → stremio_library.json")


def cmd_compare(args: argparse.Namespace) -> int:
    gaps = compute_gaps()
    common.atomic_write_json(GAPS, gaps, indent=2)
    rc = common.EXIT_WARN if gaps["unknown"] else common.EXIT_OK
    if getattr(args, "json", False):
        print(json.dumps(gaps, ensure_ascii=False, indent=1, default=list))
        return rc
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
    if gaps.get("beyond_anchor"):
        print("\nBitová mapa má díly za posledním dokoukaným (nezapisuju, ověř ručně):")
        for x in gaps["beyond_anchor"]:
            print(f"  {x['name']}: " + ", ".join(f"S{a}E{b}" for a, b in x["pairs"][:10]))
    if gaps["unknown"]:
        print(f"\nNepíšu do Traktu (mapování neověřeno, {len(gaps['unknown'])}):")
        for u in gaps["unknown"]:
            print(f"  {u['name']}: {u['reason']}")
        print("  → pro tyhle použij `forward`: doplní jen mezeru k poslednímu dokoukanému dílu")
    total = len(gaps["movies"]) + sum(len(s["missing"]) for s in gaps["shows"])
    print(f"\nCelkem chybí {total} položek.")
    print("Uloženo do gaps.json.")
    return rc


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


def cmd_push(args: argparse.Namespace) -> int:
    if not GAPS.exists():
        sys.exit("Nejdřív spusť: python3 stremio_bridge.py compare")
    gaps = json.loads(GAPS.read_text())
    # Nefiltrujeme podle pushed.json: o tom, co chybí, rozhoduje živý stav Traktu
    # (gaps.json). Opakovanému zápisu téhož brání pojistka ve writes.py.
    only = getattr(args, "only", None)
    movies = [m for m in gaps["movies"] if not only or m["imdb"] == only]
    shows = []
    skipped_specials = 0
    for s in gaps["shows"]:
        if only and s["imdb"] != only:
            continue
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
        return common.EXIT_OK

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
    return common.EXIT_WARN if (result["denied"] or result["repeat"]) else common.EXIT_OK


def load_gaps(max_age_hours: float = 12) -> dict:
    """gaps.json z posledního `compare`. Starý nebo chybějící = chyba: forward se
    rozhoduje jen podle čerstvého porovnání."""
    if not GAPS.exists() or time.time() - GAPS.stat().st_mtime > max_age_hours * 3600:
        sys.exit("gaps.json chybí nebo je starší než 12 h — nejdřív spusť: "
                 "python3 stremio_bridge.py compare")
    return json.loads(GAPS.read_text())


def cmd_forward(args: argparse.Namespace) -> int:
    """Doplní Trakt dopředu na díl, který Stremio označuje jako poslední dokoukaný.

    Jen pro seriály, které `compare` nedokázal ověřit (`gaps.json` → `unknown`).
    U ověřených seriálů zapisuje `push` přesně podle bitové mapy; `forward` by tam
    doplnil i díly, které uživatel přeskočil. Z bitové mapy se tu nebere nic než
    kotva (poslední dokoukaný díl) — doplní se mezera mezi nejvyšším dílem v Traktu
    a ní.
    """
    unknown = {u.get("imdb") for u in load_gaps()["unknown"] if u.get("imdb")}
    items = fetch_raw()
    alias = load_alias()
    rows, skipped = [], []
    for it in items:
        if it.get("type") != "series" or it.get("_id") not in unknown:
            continue
        w = (it.get("state") or {}).get("watched")
        if not w:
            continue
        name = it.get("name")
        imdb = alias.get(it.get("_id"), it.get("_id"))
        try:
            wb = decode_watched(w)
        except ValueError as e:
            skipped.append({"name": name, "imdb": imdb, "reason": str(e)})
            continue
        marker = (wb["season"], wb["episode"])
        if None in marker:
            skipped.append({"name": name, "imdb": imdb,
                            "reason": f"kotva {wb['anchor']} nemá tvar id:řada:díl"})
            continue
        inv = sorted(trakt_episode_inventory(imdb))
        if marker not in inv:
            inv = sorted(trakt_episode_inventory(imdb, refresh=True))
        if marker not in inv:
            skipped.append({"name": name, "imdb": imdb,
                            "reason": f"Stremio uvádí poslední dokoukaný S{marker[0]}E{marker[1]}, "
                                      "takový díl Trakt nezná"})
            continue
        done = trakt_watched_episodes(imdb)
        mx = max(done) if done else None
        start = inv.index(mx) + 1 if mx in inv else 0
        todo = [e for e in inv[start:inv.index(marker) + 1]
                if e not in done and (e[0] != 0 or args.include_specials)]
        if todo:
            rows.append({"name": name, "imdb": imdb, "marker": marker, "todo": todo,
                         "bits": len(wb["bits"]),
                         "last": (it.get("state") or {}).get("lastWatched")})

    rows.sort(key=lambda r: len(r["todo"]))
    selected = [r for r in rows if args.only is None or r["imdb"] == args.only]
    if args.json:
        print(json.dumps({"rows": selected, "skipped": skipped}, ensure_ascii=False, indent=1,
                         default=list))
    else:
        print(f"== Doplnění dopředu: {len(selected)} seriálů ==")
        for r in selected:
            eps = ", ".join(f"S{a}E{b}" for a, b in r["todo"])
            warn = ""
            if r["bits"] < len(r["todo"]):
                warn = f"  ⚠ Stremio má jen {r['bits']} zhlédnutých dílů – možná některé přeskočil"
            print(f"\n  {r['name']} ({r['imdb']}) — poslední dokoukaný S{r['marker'][0]}E{r['marker'][1]},"
                  f" doplnit {len(r['todo'])} dílů, naposledy {str(r['last'])[:10]}{warn}")
            print(f"    {eps[:200]}{' …' if len(eps) > 200 else ''}")
        if skipped:
            print("\n== Nešlo ==")
            for s in skipped:
                print(f"  {s['name']}: {s['reason']}")
        print(f"\nCelkem k doplnění: {sum(len(r['todo']) for r in selected)} dílů.")
    common.atomic_write_json(HERE / "forward.json", selected, indent=2)

    journal = common.Journal(LAST_FORWARD, {
        "dry_run": not args.yes, "written": [], "denied": [], "repeat": [], "skipped": skipped,
        "over_max": []})
    if not args.yes:
        if not args.json:
            print("(Dry-run. Pro zápis přidej --yes. Datum: --date unknown, výchozí = den posledního dílu.)")
        return common.EXIT_OK

    to_write = [r for r in selected if args.max is None or len(r["todo"]) <= args.max]
    journal["over_max"] = [{"imdb": r["imdb"], "name": r["name"], "todo": r["todo"],
                            "marker": r["marker"]} for r in selected if r not in to_write]
    journal.save()
    for r in journal["over_max"]:
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
    return common.EXIT_WARN if (result["denied"] or result["repeat"]) else common.EXIT_OK


def cmd_redate(args: argparse.Namespace) -> int:
    """Dá datum epizodám, které most zapsal bez data (Trakt je vede k 1. 1. 1970).

    Stremio zná jen datum posledního zhlédnutého dílu, starší díly jdou do Traktu jako
    'unknown' a nepočítají se do statistik. Tenhle příkaz u nich doplní odhad: den
    posledního dílu seriálu (nebo --date).

    Bere jen záznamy historie s datem 1970 u dílů z pushed.json — skutečné scrobbly
    a rewatche s datem nechá být. Nejdřív zapíše datovanou kopii, pak smaže přesně
    ty nedatované záznamy (podle history id, uloženo v deníku i write_log.jsonl).
    """
    pushed = common.read_json(PUSHED, {"movies": [], "episodes": []})
    groups: dict[str, set[tuple[int, int]]] = {}
    for imdb, s, e in pushed.get("episodes") or []:
        if not args.only or imdb == args.only:
            groups.setdefault(imdb, set()).add((int(s), int(e)))
    if not groups:
        sys.exit("Nic k přepisu – pushed.json je prázdný (nebo nic neodpovídá filtru).")
    alias = load_alias()
    last_by_id = {alias.get(i.get("_id"), i.get("_id")): (i.get("state") or {}).get("lastWatched")
                  for i in fetch_raw()}

    plan = []
    for imdb, pairs in sorted(groups.items()):
        when = args.date or last_by_id.get(imdb)
        if not when:
            print(f"  {imdb}: datum neznámé, přeskakuji")
            continue
        undated = [h for h in writes.fetch_show_history(imdb)
                   if (h["season"], h["episode"]) in pairs
                   and str(h["watched_at"] or "").startswith("1970-01-01")]
        if not undated:
            continue
        uniq = sorted({(h["season"], h["episode"]) for h in undated})
        print(f"  {imdb}: {len(uniq)} epizod bez data ({len(undated)} záznamů) → {when[:10]}")
        plan.append((imdb, when, undated, uniq))
    if not plan:
        print("Nic bez data.")
        return common.EXIT_OK
    if not args.yes:
        print("\n(Dry-run. Pro skutečný přepis přidej --yes.)")
        return common.EXIT_OK

    journal = common.Journal(HERE / "last_redate.json", {"shows": []})
    rc = common.EXIT_OK
    for imdb, when, undated, uniq in plan:
        row = {"imdb": imdb, "when": when, "remove_entries": undated, "added": [], "removed": [],
               "status": "started"}
        journal["shows"].append(row)
        journal.save()
        items = [writes.episode_item({"imdb": imdb}, s, e, when) for s, e in uniq]
        # stejné díly už most jednou zapsal → pojistka proti opakování tu neplatí;
        # nedatované kopie se hned potom mažou, výsledný počet zhlédnutí sedí
        res = writes.add_history(items, cmd="redate", force_repeat=True)
        row["added"] = [[i["season"], i["episode"]] for i in res["added"]]
        if len(res["added"]) != len(items):
            row["status"] = "stopped: add incomplete, nothing removed"
            journal.save()
            _report_write(res)
            rc = common.EXIT_WARN
            continue
        rres = writes.remove_history(undated, cmd="redate")
        row["removed"] = [e["history_id"] for e in rres["removed"]]
        after = [h for h in writes.fetch_show_history(imdb) if (h["season"], h["episode"]) in set(uniq)]
        still = sorted({(h["season"], h["episode"]) for h in after
                        if str(h["watched_at"] or "").startswith("1970-01-01")})
        dated_ok = {(h["season"], h["episode"]) for h in after
                    if str(h["watched_at"] or "")[:10] == when[:10]}
        missing = sorted(set(uniq) - dated_ok)
        row["status"] = "done" if not still and not missing else "done: mismatch"
        journal.save()
        print(f"    kontrola: bez data zůstává {len(still)}, s datem chybí {len(missing)} "
              f"→ {'sedí' if row['status'] == 'done' else 'NESEDÍ'}")
        if row["status"] != "done":
            rc = common.EXIT_WARN
    return rc


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


def cmd_exact(args: argparse.Namespace) -> int:
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
    try:
        st = stremio_state(item)
    except ITEM_ERRORS as e:
        # stejně jako compute_gaps: chyba dat/Cinemety = čistá hláška, nic se nemění
        print(f"{item.get('name')}: stav ze Stremia nejde přečíst ({type(e).__name__}: {e}) "
              "— nic neměním, zkus to později.", file=sys.stderr)
        sys.exit(common.EXIT_ERROR)
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
    beyond = st.get("beyond_anchor") or []
    if beyond:
        print(f"  ⚠ bitová mapa má {len(beyond)} dílů za posledním dokoukaným "
              f"({', '.join(f'S{a}E{b}' for a, b in beyond[:5])}) — nejisté")
    if (res["unmatched"] or beyond) and not args.allow_unmatched:
        print("\nNěco se nenamapovalo — nic neměním. Nenamapovaný díl by z Traktu zmizel, "
              "i když ho Stremio vede jako zhlédnutý. (--allow-unmatched to dovolí.)",
              file=sys.stderr)
        sys.exit(common.EXIT_REFUSED)
    if not args.yes:
        print("\n(Dry-run. Pro skutečnou změnu přidej --yes.)")
        return common.EXIT_OK

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
    if journal["removed"]:
        print("  vrátit jde: python3 stremio_bridge.py restore --from-journal last_exact.json")
    return common.EXIT_OK if ok else common.EXIT_WARN


def _drop_pushed(imdb: str, pairs) -> None:
    """Co `exact` z Traktu odebral, nesmí `redate` později vrátit."""
    pushed = common.read_json(PUSHED, None)
    if not pushed:
        return
    drop = {(imdb, int(s), int(e)) for s, e in pairs}
    pushed["episodes"] = [x for x in pushed.get("episodes") or [] if tuple(x) not in drop]
    common.atomic_write_json(PUSHED, pushed)


# ------------------------------------------------------------------ restore


def _sig_keys(kind: str, ids: dict, season, episode, watched_at) -> set[tuple]:
    """Podpisy záznamu pro porovnání s historií: stejný titul (podle kteréhokoli
    ID), díl a čas zhlédnutí."""
    when = str(watched_at or "")[:19]
    return {(kind, f"{k}:{ids[k]}", season, episode, when)
            for k in ("trakt", "imdb") if ids.get(k)}


def _since_ok(entry: dict, since: str | None) -> bool:
    """`--since` u všech zdrojů filtruje podle původního `watched_at`."""
    return not since or str(entry.get("watched_at") or "") >= since


def restore_candidates_from_journal(path: pathlib.Path, since: str | None = None) -> list[dict]:
    """Smazané záznamy z deníku `exact` (removed) nebo `redate` (remove_entries
    u řádků, kde se opravdu smazaly)."""
    data = common.read_json(path, None)
    if data is None:
        sys.exit(f"Deník {path} se nedá přečíst.")
    out = list(data.get("removed") or [])
    for row in data.get("shows") or []:
        gone = set(row.get("removed") or [])
        out += [e for e in row.get("remove_entries") or [] if e.get("history_id") in gone]
    return [e for e in out if _since_ok(e, since)]


def restore_candidates_from_log(since: str | None) -> list[dict]:
    return [r for r in writes.log_records()
            if r.get("op") == "remove" and _since_ok(r, since)]


def restore_candidates_from_db(path: pathlib.Path, since: str | None) -> list[dict]:
    """Záznamy, které ve snapshotu tracker.db jsou (a nebyly v něm smazané)."""
    import sqlite3
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(watched_episodes)")}
        live = " WHERE deleted_at IS NULL" if "deleted_at" in cols else ""
        out = [{"kind": "movie", "ids": {"trakt": tid, "imdb": imdb}, "watched_at": w,
                "name": title, "history_id": hid}
               for hid, tid, imdb, title, w in con.execute(
                   f"SELECT history_id, trakt_id, imdb, title, watched_at FROM watched_movies{live}")]
        out += [{"kind": "episode", "ids": {"trakt": sid}, "season": s, "episode": e,
                 "watched_at": w, "name": title, "history_id": hid}
                for hid, sid, title, s, e, w in con.execute(
                    "SELECT history_id, show_id, show_title, season, episode, watched_at "
                    f"FROM watched_episodes{live}")]
    finally:
        con.close()
    for c in out:
        c["ids"] = {k: v for k, v in c["ids"].items() if v}
    return [c for c in out if _since_ok(c, since)]


def current_history_signatures() -> set[tuple]:
    import track
    sigs: set[tuple] = set()
    for r in track.paged("/sync/history/movies"):
        ids = (r.get("movie") or {}).get("ids") or {}
        sigs |= _sig_keys("movie", ids, None, None, r.get("watched_at"))
    for r in track.paged("/sync/history/episodes"):
        ids = (r.get("show") or {}).get("ids") or {}
        ep = r.get("episode") or {}
        sigs |= _sig_keys("episode", ids, ep.get("season"), ep.get("number"), r.get("watched_at"))
    return sigs


def cmd_restore(args: argparse.Namespace) -> int:
    """Vrátí do Traktu smazané záznamy historie s původním `watched_at`.

    Zdroj: deník (`--from-journal last_exact.json`), write_log.jsonl (`--from-log`)
    nebo snapshot databáze (`--from-db zaloha.sqlite`). Co v historii Traktu už je
    (stejný titul, díl a čas), se přeskočí — opakované spuštění nic nezdvojí.
    """
    if args.from_journal:
        cands = restore_candidates_from_journal(pathlib.Path(args.from_journal).expanduser(),
                                                args.since)
    elif args.from_db:
        cands = restore_candidates_from_db(pathlib.Path(args.from_db).expanduser(), args.since)
    else:
        cands = restore_candidates_from_log(args.since)
    if args.only:
        cands = [c for c in cands if args.only in (c.get("ids") or {}).values()
                 or args.only.lower() in str(c.get("name") or "").lower()]
    if not cands:
        print("Nic k obnovení.")
        return common.EXIT_OK

    present = current_history_signatures()
    todo, seen = [], set()
    for c in cands:
        sig = _sig_keys(c["kind"], c.get("ids") or {}, c.get("season"), c.get("episode"),
                        c.get("watched_at"))
        if not sig or sig & present or frozenset(sig) in seen:
            continue
        seen.add(frozenset(sig))
        todo.append(c)

    print(f"Kandidátů {len(cands)}, v Traktu chybí {len(todo)}:")
    for c in todo[:30]:
        what = f"S{c['season']}E{c['episode']}" if c["kind"] == "episode" else "film"
        print(f"  {c.get('name') or writes._id_key(c['ids'])} {what} — {str(c.get('watched_at'))[:16]}")
    if len(todo) > 30:
        print(f"  … a dalších {len(todo) - 30}")
    if not todo or not args.yes:
        if todo:
            print("\n(Dry-run. Pro obnovení přidej --yes.)")
        return common.EXIT_OK

    items = []
    for c in todo:
        when = c.get("watched_at")
        when = None if not when or str(when).startswith("1970-01-01") else when
        if c["kind"] == "movie":
            items.append(writes.movie_item(c["ids"], when, c.get("name")))
        else:
            items.append(writes.episode_item(c["ids"], c["season"], c["episode"], when, c.get("name")))
    journal = common.Journal(HERE / "last_restore.json", {"planned": len(items), "added": 0,
                                                           "denied": [], "repeat": []})

    def progress(res: dict) -> None:
        journal["added"] = len(res["added"])
        journal["denied"] = list(res["denied"])
        journal["repeat"] = [writes.item_key(i) for i in res["repeat"]]
        journal.save()

    result = writes.add_history(items, cmd="restore", force_repeat=args.force_repeat,
                                on_progress=progress)
    progress(result)
    print(f"  obnoveno {len(result['added'])} z {len(items)}")
    _report_write(result)
    return common.EXIT_WARN if (result["denied"] or result["repeat"]) else common.EXIT_OK


def main() -> None:
    ap = argparse.ArgumentParser(description="Most Stremio → Trakt")
    sub = ap.add_subparsers(dest="cmd", required=True)
    lg = sub.add_parser("_login")
    lg.add_argument("email")
    lg.set_defaults(func=cmd__login)
    sub.add_parser("list", help="co je ve Stremio knihovně").set_defaults(func=cmd_list)
    sub.add_parser("fetch", help="stáhne knihovnu ze Stremia").set_defaults(func=cmd_fetch)
    cp = sub.add_parser("compare", help="rozdíl proti Traktu")
    cp.add_argument("--json", action="store_true", help="výstup jako JSON")
    cp.set_defaults(func=cmd_compare)
    pu = sub.add_parser("push", help="doplnit chybějící do Traktu")
    pu.add_argument("--yes", action="store_true", help="skutečně zapsat")
    pu.add_argument("--only", help="jen jeden titul (IMDb ID v Traktu)")
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
    fw.add_argument("--json", action="store_true", help="výstup jako JSON")
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
    rs = sub.add_parser("restore", help="vrátí do Traktu smazané záznamy s původním datem")
    src = rs.add_mutually_exclusive_group()
    src.add_argument("--from-journal", help="deník exact/redate (např. last_exact.json)")
    src.add_argument("--from-log", action="store_true", help="odebrání z write_log.jsonl (výchozí)")
    src.add_argument("--from-db", help="snapshot tracker.db ze zálohy")
    rs.add_argument("--since", help="jen záznamy zhlédnuté od tohoto data (ISO, podle watched_at)")
    rs.add_argument("--only", help="jen titul (ID nebo část názvu)")
    rs.add_argument("--yes", action="store_true", help="skutečně zapsat")
    rs.add_argument("--force-repeat", action="store_true",
                    help="zapsat i to, co se zapsalo v posledních dnech")
    rs.set_defaults(func=cmd_restore)
    args = ap.parse_args()
    if getattr(args, "yes", False):
        with common.lock():         # zápisy do Traktu nikdy dva najednou
            rc = args.func(args)
    else:
        rc = args.func(args)
    sys.exit(rc or common.EXIT_OK)


if __name__ == "__main__":
    main()
