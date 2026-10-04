#!/usr/bin/env python3
"""Trakt tracker – lokální zrcadlo Trakt historie + přehledy.

Použití:
  python3 track.py auth            # jednorázové přihlášení (device flow)
  python3 track.py sync            # stáhne historii/hodnocení/watchlist do tracker.db
  python3 track.py report          # měsíční přehled (markdown)
  python3 track.py report --year   # roční přehled
  python3 track.py status          # co je v DB a kdy se naposledy synchronizovalo

Konfigurace: config.json v této složce (client_id + client_secret, práva 600).
Token se ukládá do stejného souboru a automaticky se obnovuje. Volitelný blok
"settings" přepisuje výchozí hodnoty z common.DEFAULTS.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import common

BASE = "https://api.trakt.tv"
HERE = common.DATA_DIR
CONFIG = HERE / "config.json"
DB = HERE / "tracker.db"
UA = "stremio-trakt-track/2.0"

# ---------------------------------------------------------------- konfigurace


def load_config() -> dict:
    if not CONFIG.exists():
        sys.exit(f"Chybí {CONFIG}. Spusť nejdřív setup_secret.sh.")
    return json.loads(CONFIG.read_text())


def save_config(cfg: dict) -> None:
    # atomicky a rovnou 600: pád uprostřed zápisu nesmí smazat token
    common.atomic_write_json(CONFIG, cfg, indent=2)


# --------------------------------------------------------------------- HTTP


class TraktError(RuntimeError):
    pass


RETRY_STATUS = {500, 502, 503, 504, 520, 521, 522, 523, 524, 525, 526, 527, 530}
AUTH_STATE = HERE / "auth_state.json"
REFRESH_MARGIN = 86400          # obnov token, když do vypršení zbývá méně než den


def _req(method: str, path: str, params: dict | None = None,
         body: dict | None = None, auth: bool = True,
         cfg: dict | None = None) -> tuple[object, dict]:
    """Jedno volání Trakt API.

    Přechodné chyby (5xx, timeout, výpadek sítě) se u čtení (GET) zkouší znovu
    s odstupem `retry_delays`; teprve pak TraktError. Zápisy (POST) se po
    nejednoznačné chybě znovu neposílají: request mohl projít a Trakt by zápis
    započítal dvakrát. Zbytek doplní další běh podle živého stavu. 429 (limit) se
    čeká podle Retry-After u obou — Trakt ho vrací dřív, než request zpracuje.
    """
    cfg = cfg or load_config()
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {
        "Content-Type": "application/json",
        "User-Agent": UA,
        "trakt-api-version": "2",
        "trakt-api-key": cfg["client_id"],
    }
    if auth:
        headers["Authorization"] = "Bearer " + access_token()
    data = json.dumps(body).encode() if body is not None else None
    delays = common.retry_delays()
    retryable = method == "GET"
    last_error = ""

    for attempt in range(len(delays) + 1):
        wait = delays[attempt] if attempt < len(delays) else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                payload = json.loads(raw) if raw else None
                # Trakt posílá hlavičky malými písmeny → normalizuj klíče
                return payload, {k.lower(): v for k, v in resp.headers.items()}
        except urllib.error.HTTPError as e:
            raw = e.read()
            detail = raw[:300].decode(errors="replace")
            if e.code == 429 and wait is not None:
                try:
                    wait = min(60.0, float(e.headers.get("Retry-After") or wait))
                except ValueError:
                    pass
                print(f"  rate limit, čekám {wait:.0f}s…", file=sys.stderr)
                common.sleep(wait + 1)
                last_error = "HTTP 429 (limit)"
                continue
            if e.code == 423:
                raise TraktError("Účet je zamčený – spusť history analysis na trakt.tv/settings/data.") from None
            if e.code == 426:
                raise TraktError("Tahle metoda je VIP-only.") from None
            if e.code == 401 and auth:
                raise TraktError(f"{method} {path} -> HTTP 401: přihlášení k Traktu neplatí "
                                 "(náprava: python3 track.py auth)") from None
            if e.code in RETRY_STATUS and retryable and wait is not None:
                last_error = f"HTTP {e.code}"
                common.sleep(wait)
                continue
            raise TraktError(f"{method} {path} -> HTTP {e.code}: {detail}") from None
        except urllib.error.URLError as e:
            last_error = f"Síťová chyba u {path}: {e.reason}"
        except (TimeoutError, OSError) as e:
            # timeout while reading the body is not wrapped in URLError
            last_error = f"Síťová chyba u {path}: {e}"
        except ValueError as e:
            raise TraktError(f"{method} {path}: neplatná odpověď ({e})") from None
        if not retryable or wait is None:
            raise TraktError(last_error)
        common.sleep(wait)
    raise TraktError(f"{method} {path}: nepovedlo se ani po {len(delays) + 1} pokusech ({last_error})")


def _auth_fail(msg: str) -> None:
    print(msg, file=sys.stderr)
    print("Náprava: python3 track.py auth", file=sys.stderr)
    sys.exit(common.EXIT_AUTH)


def _note_refresh(ok: bool, error: str = "") -> None:
    state = common.read_json(AUTH_STATE, {})
    stamp = dt.datetime.fromtimestamp(common.now()).astimezone().isoformat(timespec="seconds")
    if ok:
        state.update({"refresh_ok_at": stamp, "refresh_ok_epoch": common.now()})
        state.pop("refresh_failed_at", None)
        state.pop("refresh_error", None)
    else:
        state.update({"refresh_failed_at": stamp, "refresh_error": error})
    common.atomic_write_json(AUTH_STATE, state)


def token_remaining(cfg: dict | None = None) -> float | None:
    tok = (cfg or load_config()).get("token") or {}
    if not tok.get("access_token"):
        return None
    return float(tok.get("expires_at") or 0) - common.now()


def access_token() -> str:
    """Platný access token. Nikdy nic interaktivního.

    Obnova začne, když do vypršení zbývá méně než REFRESH_MARGIN — tedy ještě
    s platným tokenem. Když obnova selže a token pořád platí, běh pokračuje,
    selhání se zapíše do auth_state.json (cron ho ohlásí) a na stderr jde náprava.
    Když token už neplatí, příkaz skončí s EXIT_AUTH.
    """
    cfg = load_config()
    tok = cfg.get("token") or {}
    if not tok.get("access_token"):
        _auth_fail("Trakt: nejsi přihlášený.")
    remaining = float(tok.get("expires_at") or 0) - common.now()
    if remaining < REFRESH_MARGIN:
        try:
            refresh_token()
        except TraktError as e:
            _note_refresh(False, str(e))
            if remaining > 0:
                print(f"! Obnovení Trakt tokenu selhalo ({e}); token platí ještě "
                      f"{remaining / 3600:.0f} h. Náprava: python3 track.py auth", file=sys.stderr)
                return tok["access_token"]
            _auth_fail(f"Trakt token vypršel a obnovení selhalo ({e}).")
        tok = load_config().get("token") or {}
    return tok["access_token"]


def refresh_token() -> None:
    cfg = load_config()
    tok = cfg.get("token") or {}
    if not tok.get("refresh_token"):
        raise TraktError("chybí refresh token")
    body = {
        "grant_type": "refresh_token",
        "refresh_token": tok.get("refresh_token"),
        "client_id": cfg["client_id"],
        "redirect_uri": cfg.get("redirect_uri") or "urn:ietf:wg:oauth:2.0:oob",
    }
    if cfg.get("client_secret"):
        body["client_secret"] = cfg["client_secret"]
    payload, _ = _req("POST", "/oauth/token", body=body, auth=False, cfg=cfg)
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise TraktError("obnova tokenu nevrátila access_token")
    store_token(cfg, payload)
    _note_refresh(True)
    print("Token obnoven.", file=sys.stderr)


def store_token(cfg: dict, payload: dict) -> None:
    expires_in = int(payload.get("expires_in") or 7776000)
    cfg["token"] = {
        "access_token": payload["access_token"],
        "refresh_token": payload.get("refresh_token"),
        "expires_at": int(common.now()) + expires_in,
        "created_at": payload.get("created_at") or dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    save_config(cfg)


# --------------------------------------------------------------------- auth


def cmd_auth(_args: argparse.Namespace) -> None:
    common.require_interactive("Přihlášení k Traktu (device flow)", "python3 track.py auth v terminálu")
    cfg = load_config()
    payload, _ = _req("POST", "/oauth/device/code",
                      body={"client_id": cfg["client_id"]}, auth=False, cfg=cfg)
    code = payload["user_code"]
    url = payload.get("verification_url", "https://trakt.tv/activate")
    interval = int(payload.get("interval") or 5)
    expires = int(payload.get("expires_in") or 600)

    print("=" * 58, flush=True)
    print(f"  1) Otevři {url}", flush=True)
    print(f"  2) Zadej kód:  {code}", flush=True)
    print(f"  3) Potvrď aplikaci.  (kód platí {expires // 60} minut)", flush=True)
    print("=" * 58, flush=True)
    print("Čekám na potvrzení…", flush=True)

    device_code = payload["device_code"]
    common.atomic_write_json(HERE / ".device.json",
                             {"user_code": code, "verification_url": url,
                              "expires_at": int(time.time()) + expires}, indent=2)
    deadline = time.time() + expires
    while time.time() < deadline:
        time.sleep(interval)
        try:
            body = {"code": device_code, "client_id": cfg["client_id"]}
            if cfg.get("client_secret"):
                body["client_secret"] = cfg["client_secret"]
            tok, _ = _req("POST", "/oauth/device/token", body=body, auth=False, cfg=cfg)
        except TraktError as e:
            msg = str(e)
            if "HTTP 400" in msg:      # ještě nepotvrzeno
                continue
            if "HTTP 429" in msg:
                interval += 1
                continue
            if "HTTP 410" in msg:
                sys.exit("Kód vypršel. Spusť auth znovu.")
            if "HTTP 418" in msg:
                sys.exit("Potvrzení zamítnuto na trakt.tv.")
            if "HTTP 409" in msg:
                sys.exit("Kód už byl použit. Spusť auth znovu.")
            raise
        store_token(cfg, tok)
        print("Hotovo – přihlášeno. Teď můžeš spustit: python3 track.py sync")
        return
    sys.exit("Kód vypršel. Spusť auth znovu.")


# --------------------------------------------------------------------- sync


def paged(path: str, params: dict | None = None) -> list:
    """Projde všechny stránky a vrátí spojený seznam."""
    out: list = []
    page = 1
    while True:
        p = dict(params or {})
        p.update({"page": page, "limit": 100})
        payload, headers = _req("GET", path, params=p)
        if isinstance(payload, list):
            out.extend(payload)
        else:
            return [payload] if payload else []
        pages = int(headers.get("x-pagination-page-count") or 0)
        if (pages and page >= pages) or len(payload) < p["limit"]:
            return out
        page += 1


DDL = """
CREATE TABLE IF NOT EXISTS watched_movies(
  history_id INTEGER PRIMARY KEY, trakt_id INTEGER, title TEXT, year INTEGER,
  watched_at TEXT, action TEXT, imdb TEXT, tmdb TEXT, runtime INTEGER);
CREATE TABLE IF NOT EXISTS watched_episodes(
  history_id INTEGER PRIMARY KEY, show_id INTEGER, show_title TEXT,
  season INTEGER, episode INTEGER, ep_title TEXT, watched_at TEXT,
  action TEXT, runtime INTEGER);
CREATE TABLE IF NOT EXISTS ratings(
  kind TEXT, trakt_id INTEGER, title TEXT, season INTEGER, episode INTEGER,
  rating INTEGER, rated_at TEXT,
  PRIMARY KEY (kind, trakt_id, season, episode));
CREATE TABLE IF NOT EXISTS watchlist(
  kind TEXT, trakt_id INTEGER, title TEXT, year INTEGER, listed_at TEXT,
  PRIMARY KEY (kind, trakt_id));
CREATE TABLE IF NOT EXISTS shows_progress(
  trakt_id INTEGER PRIMARY KEY, title TEXT, aired INTEGER, completed INTEGER,
  last_watched_at TEXT, next_season INTEGER, next_number INTEGER, next_title TEXT,
  hidden_seasons TEXT);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""

# Historie je archiv: co z Traktu zmizí, se jen označí `deleted_at` a přehledy
# čtou pohledy live_*. Smazané řádky zůstávají jako doklad a dají se vrátit.
VIEWS = """
CREATE VIEW IF NOT EXISTS live_movies AS SELECT * FROM watched_movies WHERE deleted_at IS NULL;
CREATE VIEW IF NOT EXISTS live_episodes AS SELECT * FROM watched_episodes WHERE deleted_at IS NULL;
"""

MOVIE_COLS = "history_id, trakt_id, title, year, watched_at, action, imdb, tmdb, runtime"
EPISODE_COLS = ("history_id, show_id, show_title, season, episode, ep_title, watched_at, "
                "action, runtime")


def db(path: pathlib.Path | None = None) -> sqlite3.Connection:
    con = sqlite3.connect(path or DB)
    con.executescript(DDL)
    _migrate(con)
    return con


def _migrate(con: sqlite3.Connection) -> None:
    for table in ("watched_movies", "watched_episodes"):
        cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        if "deleted_at" not in cols:
            con.execute(f"ALTER TABLE {table} ADD COLUMN deleted_at TEXT")
    con.executescript(VIEWS)
    if con.execute("PRAGMA user_version").fetchone()[0] < 1:
        # NULL v primárním klíči nefunguje jako rovnost → sjednoť na -1 a zahoď duplicity
        con.execute("DELETE FROM ratings WHERE rowid NOT IN (SELECT MIN(rowid) FROM ratings "
                    "GROUP BY kind, trakt_id, COALESCE(season,-1), COALESCE(episode,-1))")
        con.execute("UPDATE ratings SET season=COALESCE(season,-1), episode=COALESCE(episode,-1)")
        con.execute("PRAGMA user_version = 1")
    con.commit()


def set_meta(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, value))


class PruneRefused(Exception):
    pass


def _prune(con: sqlite3.Connection, table: str, column: str, live: set, label: str,
           *, soft: bool, allow_mass: bool = False) -> int:
    """Zrcadlo, ne hromada: co v Traktu už není, nesmí se počítat v přehledech.

    Historie (`soft=True`) se jen označí `deleted_at`, ostatní se maže. Když by
    zmizelo víc než `prune_max_rows` řádků a zároveň víc než `prune_max_fraction`
    tabulky, je to spíš výpadek nebo chyba Traktu než úklid → PruneRefused
    (sync pak skončí s EXIT_GUARD a cron to ohlásí). `--allow-mass-delete` projde.
    """
    where = " WHERE deleted_at IS NULL" if soft else ""
    rows = [r[0] for r in con.execute(f"SELECT {column} FROM {table}{where}").fetchall()]
    stale = [x for x in rows if x not in live]
    if not stale:
        return 0
    st = common.settings()
    if (not allow_mass and len(stale) > st["prune_max_rows"]
            and len(stale) > st["prune_max_fraction"] * len(rows)):
        raise PruneRefused(f"{label}: v Traktu chybí {len(stale)} z {len(rows)} záznamů — "
                           "neprořezávám (výpadek Traktu?). Pokud je to záměr: "
                           "track.py sync --allow-mass-delete")
    if soft:
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        con.executemany(f"UPDATE {table} SET deleted_at=? WHERE {column}=?",
                        [(now, i) for i in stale])
        print(f"  označeno {len(stale)} záznamů, které v Traktu už nejsou ({label})")
    else:
        con.executemany(f"DELETE FROM {table} WHERE {column}=?", [(i,) for i in stale])
        print(f"  smazáno {len(stale)} záznamů, které v Traktu už nejsou ({label})")
    return len(stale)


def cmd_sync(args: argparse.Namespace) -> None:
    con = db()
    allow_mass = bool(getattr(args, "allow_mass_delete", False))
    refused: list[str] = []

    def prune(*a, **kw) -> None:
        try:
            _prune(con, *a, allow_mass=allow_mass, **kw)
        except PruneRefused as e:
            refused.append(str(e))

    me = _req("GET", "/users/me")[0]
    user = me.get("username")
    print(f"Účet: {user}")

    print("Historie filmů…")
    movies = paged("/sync/history/movies", {"extended": "full"})
    for it in movies:
        m = it.get("movie") or {}
        con.execute(
            f"INSERT OR REPLACE INTO watched_movies({MOVIE_COLS}) VALUES(?,?,?,?,?,?,?,?,?)",
            (it.get("id"), m.get("ids", {}).get("trakt"), m.get("title"), m.get("year"),
             it.get("watched_at"), it.get("action"),
             m.get("ids", {}).get("imdb"), m.get("ids", {}).get("tmdb"), m.get("runtime")))
    print(f"  {len(movies)} záznamů")
    prune("watched_movies", "history_id", {it.get("id") for it in movies}, "filmů", soft=True)

    print("Historie epizod…")
    eps = paged("/sync/history/episodes", {"extended": "full"})
    for it in eps:
        ep = it.get("episode") or {}
        show = it.get("show") or {}
        con.execute(
            f"INSERT OR REPLACE INTO watched_episodes({EPISODE_COLS}) VALUES(?,?,?,?,?,?,?,?,?)",
            (it.get("id"), show.get("ids", {}).get("trakt"), show.get("title"),
             ep.get("season"), ep.get("number"), ep.get("title"), it.get("watched_at"),
             it.get("action"), ep.get("runtime")))
    print(f"  {len(eps)} záznamů")
    prune("watched_episodes", "history_id", {it.get("id") for it in eps}, "epizod", soft=True)

    print("Hodnocení…")
    total = 0
    seen_ratings: set = set()
    ratings_complete = True
    for kind, path in (("movie", "/sync/ratings/movies"), ("show", "/sync/ratings/shows"),
                       ("season", "/sync/ratings/seasons"), ("episode", "/sync/ratings/episodes")):
        try:
            items = paged(path)
        except TraktError as e:
            print(f"  {kind}: přeskočeno ({e})")
            ratings_complete = False
            continue
        for it in items:
            obj = it.get(kind) or it.get("movie") or it.get("show") or it.get("episode") or {}
            if kind == "season":
                show = it.get("show") or {}
                con.execute("INSERT OR REPLACE INTO ratings VALUES(?,?,?,?,?,?,?)",
                            (kind, show.get("ids", {}).get("trakt"), show.get("title"),
                             obj.get("number"), -1, it.get("rating"), it.get("rated_at")))
                seen_ratings.add((kind, show.get("ids", {}).get("trakt"), obj.get("number"), -1))
            elif kind == "episode":
                show = it.get("show") or {}
                con.execute("INSERT OR REPLACE INTO ratings VALUES(?,?,?,?,?,?,?)",
                            (kind, show.get("ids", {}).get("trakt"), show.get("title"),
                             obj.get("season"), obj.get("number"), it.get("rating"), it.get("rated_at")))
                seen_ratings.add((kind, show.get("ids", {}).get("trakt"), obj.get("season"), obj.get("number")))
            else:
                con.execute("INSERT OR REPLACE INTO ratings VALUES(?,?,?,?,?,?,?)",
                            (kind, obj.get("ids", {}).get("trakt"), obj.get("title"),
                             -1, -1, it.get("rating"), it.get("rated_at")))
                seen_ratings.add((kind, obj.get("ids", {}).get("trakt"), -1, -1))
            total += 1
    print(f"  {total} hodnocení")
    if ratings_complete:
        stale = [tuple(r) for r in con.execute(
            "SELECT kind, trakt_id, season, episode FROM ratings").fetchall()
            if tuple(r) not in seen_ratings]
        if stale:
            con.executemany("DELETE FROM ratings WHERE kind=? AND trakt_id=? AND season=? AND episode=?",
                            stale)
            print(f"  smazáno {len(stale)} hodnocení, která v Traktu už nejsou")

    print("Watchlist…")
    wl = 0
    seen_wl: set = set()
    wl_complete = True
    for kind, path in (("movie", "/sync/watchlist/movies"), ("show", "/sync/watchlist/shows")):
        try:
            items = paged(path, {"extended": "full"})
        except TraktError as e:
            print(f"  {kind}: přeskočeno ({e})")
            wl_complete = False
            continue
        for it in items:
            obj = it.get(kind) or {}
            con.execute("INSERT OR REPLACE INTO watchlist VALUES(?,?,?,?,?)",
                        (kind, obj.get("ids", {}).get("trakt"), obj.get("title"),
                         obj.get("year"), it.get("listed_at")))
            seen_wl.add((kind, obj.get("ids", {}).get("trakt")))
            wl += 1
    print(f"  {wl} položek")
    if wl_complete:
        stale = [tuple(r) for r in con.execute("SELECT kind, trakt_id FROM watchlist").fetchall()
                 if tuple(r) not in seen_wl]
        if stale:
            con.executemany("DELETE FROM watchlist WHERE kind=? AND trakt_id=?", stale)
            print(f"  smazáno {len(stale)} položek watchlistu, které v Traktu už nejsou")

    print("Postup u seriálů…")
    shows = paged("/users/me/watched/shows", {"extended": "noseasons"})
    for i, it in enumerate(shows, 1):
        show = it.get("show") or {}
        sid = show.get("ids", {}).get("trakt")
        try:
            pr = _req("GET", f"/shows/{sid}/progress/watched",
                      params={"hidden": "false", "specials": "true"})[0]
        except TraktError as e:
            print(f"  ! {show.get('title')}: {e}")
            continue
        nxt = pr.get("next_episode") or {}
        con.execute("INSERT OR REPLACE INTO shows_progress VALUES(?,?,?,?,?,?,?,?,?)",
                    (sid, show.get("title"), pr.get("aired"), pr.get("completed"),
                     it.get("last_watched_at"), nxt.get("season"), nxt.get("number"),
                     nxt.get("title"), json.dumps(pr.get("hidden_seasons") or [])))
        if i % 25 == 0:
            print(f"  … {i}/{len(shows)}")
    print(f"  {len(shows)} seriálů")
    prune("shows_progress", "trakt_id",
          {(it.get("show") or {}).get("ids", {}).get("trakt") for it in shows}, "seriálů",
          soft=False)

    stats = _req("GET", "/users/me/stats")[0]
    set_meta(con, "stats", json.dumps(stats))
    set_meta(con, "username", user or "")
    set_meta(con, "synced_at", dt.datetime.now().astimezone().isoformat(timespec="seconds"))
    con.commit()
    con.close()
    if refused:
        for r in refused:
            print(f"! {r}", file=sys.stderr)
        sys.exit(common.EXIT_GUARD)
    print("Synchronizováno.")


# ------------------------------------------------------------------ report


def parse_when(iso: str | None) -> dt.datetime | None:
    if not iso:
        return None
    try:
        return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None


def dated(iso: str | None) -> dt.datetime | None:
    """Místní čas záznamu; None u „neznámého“ data (Trakt ho vede k 1. 1. 1970)."""
    return parse_when(iso) if (iso or "") >= "2000" else None


cn = common.cn


def cmd_report(args: argparse.Namespace) -> None:
    if getattr(args, "all", False):
        return cmd_report_all(args)
    con = db()
    row = con.execute("SELECT value FROM meta WHERE key='synced_at'").fetchone()
    synced = row[0] if row else "nikdy"
    row = con.execute("SELECT value FROM meta WHERE key='username'").fetchone()
    who = row[0] if row else "?"

    today = dt.datetime.now().astimezone()
    if args.year:
        start = dt.datetime(today.year, 1, 1, tzinfo=today.tzinfo)
        end = dt.datetime(today.year + 1, 1, 1, tzinfo=today.tzinfo)
        label = f"rok {today.year}"
    else:
        ym = args.month or today.strftime("%Y-%m")
        y, m = (int(x) for x in ym.split("-"))
        start = dt.datetime(y, m, 1, tzinfo=today.tzinfo)
        end = dt.datetime(y + (m == 12), (m % 12) + 1, 1, tzinfo=today.tzinfo)
        label = f"{ym}"

    lo, hi = common.utc_bound(start), common.utc_bound(end)
    movies = con.execute(
        "SELECT title, year, watched_at FROM live_movies WHERE watched_at>=? AND watched_at<? "
        "ORDER BY watched_at", (lo, hi)).fetchall()
    eps = con.execute(
        "SELECT show_title, season, episode, ep_title, watched_at, runtime FROM live_episodes "
        "WHERE watched_at>=? AND watched_at<? ORDER BY watched_at", (lo, hi)).fetchall()
    rts = con.execute(
        "SELECT kind, title, season, episode, rating FROM ratings WHERE rated_at>=? AND rated_at<? "
        "GROUP BY kind, title, season, episode ORDER BY MAX(rated_at)", (lo, hi)).fetchall()

    minutes = sum((e[5] or 0) for e in eps)
    movies_min = 0
    if movies:
        mm = con.execute("SELECT SUM(runtime) FROM live_movies WHERE watched_at>=? AND watched_at<?",
                         (lo, hi)).fetchone()[0]
        movies_min = mm or 0
    minutes += movies_min

    print(f"## Přehled sledování — {label}")
    print()
    print(f"*účet {who}, data k {synced}*")
    print()
    print(f"- **{cn(len(movies), 'film', 'filmy', 'filmů')}** a "
          f"**{cn(len(eps), 'epizoda', 'epizody', 'epizod')}**")
    if minutes:
        print(f"- celkem cca **{minutes // 60} h {minutes % 60} min**")
    if movies or eps:
        days = set()
        for _, _, d in movies:
            p = parse_when(d)
            if p:
                days.add(p.date())
        for e in eps:
            p = parse_when(e[4])
            if p:
                days.add(p.date())
        print(f"- koukáno {len(days)} dní")
    print()

    if movies:
        print("### Filmy")
        agg: dict[tuple, list] = {}
        for title, year, when in movies:
            agg.setdefault((title, year), []).append(when)
        for (title, year), whens in agg.items():
            days = sorted(p for p in (parse_when(w) for w in whens) if p)
            first = days[0].strftime("%-d. %-m.") if days else "?"
            if len(days) > 1:
                cnt = f" — {len(days)}× ({days[-1].strftime('%-d. %-m.')} naposled)"
            elif len(whens) > 1:
                cnt = f" — {len(whens)}×"
            else:
                cnt = ""
            print(f"- {first} — **{title}** ({year}){cnt}")
        print()

    if eps:
        print("### Seriály")
        by_show: dict[str, list] = {}
        for show, s, e, et, when, rt in eps:
            by_show.setdefault(show, []).append((s, e, et, when))
        for show, items in sorted(by_show.items(), key=lambda kv: -len(kv[1])):
            seasons = sorted({s for s, _, _, _ in items})
            first = min((parse_when(w) for *_, w in items if parse_when(w)), default=None)
            last = max((parse_when(w) for *_, w in items if parse_when(w)), default=None)
            span = ""
            if first and last:
                span = f", {first.strftime('%-d.%-m.')}–{last.strftime('%-d.%-m.')}" if first.date() != last.date() else f", {first.strftime('%-d.%-m.')}"
            sea = f"S{','.join(str(s) for s in seasons)}" if seasons != [0] else "speciály"
            print(f"- **{show}** — {cn(len(items), 'epizoda', 'epizody', 'epizod')} ({sea}{span})")
        print()

    unfinished = con.execute(
        "SELECT title, aired, completed, last_watched_at, next_season, next_number, next_title "
        "FROM shows_progress WHERE completed < aired AND aired > 0 "
        "ORDER BY last_watched_at DESC LIMIT 12").fetchall()
    if unfinished:
        print("### Kde jsme skončili")
        for title, aired, completed, lw, ns, nn, nt in unfinished:
            nxt = f" → S{ns}E{nn}" if ns else ""
            nts = f" „{nt}“" if nt else ""
            p = parse_when(lw)
            when = f", naposledy {p.strftime('%-d. %-m.')}" if p else ""
            print(f"- **{title}** — {completed}/{aired} zhlédnuto{nxt}{nts}{when}")
        print()

    if rts:
        print("### Hodnocení")
        for kind, title, s, e, rating in rts:
            extra = ""
            if kind == "episode":
                extra = f" S{s}E{e}"
            elif kind == "season":
                extra = f" S{s}"
            print(f"- {rating}/10 — {title}{extra} ({kind})")
        print()

    print("---")
    print("Vygenerováno lokálním trackerem z `tracker.db`.")
    con.close()


def cmd_report_all(_args: argparse.Namespace) -> None:
    """Celoživotní přehled z tracker.db.

    Trakt `/users/me/stats` umí vrátit null, takže se všechno počítá
    z historie: počty záznamů, unikátní tituly, hodiny (z runtime jednotlivých
    záznamů), dny a roky.
    """
    con = db()
    row = con.execute("SELECT value FROM meta WHERE key='synced_at'").fetchone()
    synced = row[0] if row else "nikdy"
    row = con.execute("SELECT value FROM meta WHERE key='username'").fetchone()
    who = row[0] if row else "?"

    mv = con.execute(
        "SELECT COUNT(*), COUNT(DISTINCT title || '|' || COALESCE(year, 0)), "
        "MIN(watched_at) FILTER (WHERE watched_at >= '2000-01-01'), "
        "MAX(watched_at) FILTER (WHERE watched_at >= '2000-01-01'), "
        "SUM(runtime) FROM live_movies").fetchone()
    ep = con.execute(
        "SELECT COUNT(*), COUNT(DISTINCT show_title || '|' || season || '|' || episode), "
        "MIN(watched_at) FILTER (WHERE watched_at >= '2000-01-01'), "
        "MAX(watched_at) FILTER (WHERE watched_at >= '2000-01-01'), "
        "SUM(runtime) FROM live_episodes").fetchone()
    minutes = int(mv[4] or 0) + int(ep[4] or 0)

    dated = con.execute(
        "SELECT COUNT(*) FROM live_movies WHERE watched_at >= '2000-01-01'").fetchone()[0]
    dated += con.execute(
        "SELECT COUNT(*) FROM live_episodes WHERE watched_at >= '2000-01-01'").fetchone()[0]
    undated = (mv[0] + ep[0]) - dated
    rows = [("m", w, rt) for w, rt in con.execute("SELECT watched_at, runtime FROM live_movies")]
    rows += [("e", w, rt) for w, rt in con.execute("SELECT watched_at, runtime FROM live_episodes")]
    # dny a roky podle místního času, stejně jako měsíční přehled
    days = len({p.date() for _k, w, _rt in rows if (p := dated(w))})

    print(f"## Celoživotní přehled — účet {who}")
    print()
    print(f"*data k {synced}*")
    print()
    print(f"- **{cn(mv[1], 'film', 'filmy', 'filmů')}** ({mv[0]} záznamů) a "
          f"**{cn(ep[0], 'epizoda', 'epizody', 'epizod')}**")
    print(f"- celkem cca **{minutes // 60} h {minutes % 60} min**")
    starts = [x for x in (parse_when(mv[2]), parse_when(ep[2])) if x]
    ends = [x for x in (parse_when(mv[3]), parse_when(ep[3])) if x]
    p1 = min(starts) if starts else None
    p2 = max(ends) if ends else None
    if p1 and p2:
        print(f"- od {p1.strftime('%-d. %-m. %Y')} do {p2.strftime('%-d. %-m. %Y')}, "
              f"koukáno {days} dní")
    if undated:
        print(f"- {undated} záznamů bez data (sledováno, ale neví se kdy — Trakt je vede k 1. 1. 1970)")
    print()

    print("### Podle let")
    years: dict[str, list[int]] = {}
    undated_years = [0, 0, 0]
    for kind, w, rt in rows:
        p = dated(w)
        acc = years.setdefault(str(p.year), [0, 0, 0]) if p else undated_years
        acc[0 if kind == "m" else 1] += 1
        acc[2] += int(rt or 0)
    real_years = [(y, *v) for y, v in years.items()]
    if undated_years[0] or undated_years[1]:
        print(f"- **bez data**: {undated_years[0]} filmů, {undated_years[1]} epizod, "
              f"cca {undated_years[2] // 60} h")
    for y, m, e, mins in sorted(real_years):
        print(f"- **{y}**: {m} filmů, {e} epizod, cca {mins // 60} h")
    print()

    print("### Nejvíc zhlédnuté seriály")
    for t, n in con.execute(
            "SELECT show_title, COUNT(DISTINCT season || '|' || episode) FROM live_episodes "
            "GROUP BY show_title ORDER BY 2 DESC LIMIT 10"):
        print(f"- **{t}** — {cn(n, 'díl', 'díly', 'dílů')}")
    print()

    print("### Filmy víckrát")
    for t, n in con.execute("SELECT title, COUNT(*) FROM live_movies GROUP BY title "
                            "HAVING COUNT(*) > 1 ORDER BY 2 DESC LIMIT 8"):
        print(f"- **{t}** — {n}×")
    print()

    done = con.execute("SELECT COUNT(*) FROM shows_progress WHERE completed >= aired AND aired > 0").fetchone()[0]
    total_shows = con.execute("SELECT COUNT(*) FROM shows_progress").fetchone()[0]
    finished = con.execute("SELECT title, completed FROM shows_progress "
                           "WHERE completed >= aired AND aired > 0 ORDER BY completed DESC LIMIT 5").fetchall()
    print(f"### Seriály: {total_shows} rozkoukaných, {done} dokoukaných")
    for t, c in finished:
        print(f"- **{t}** — {cn(c, 'díl', 'díly', 'dílů')}")
    print()
    print("---")
    print("Vygenerováno lokálním trackerem z `tracker.db`.")
    con.close()


def cmd_auth_check(_args: argparse.Namespace) -> None:
    """Neinteraktivní kontrola přihlášení pro cron: obnoví token s předstihem
    a ověří ho voláním API. rc 0 = funguje, 5 = potřebuje člověka, 1 = síť."""
    access_token()
    try:
        me = _req("GET", "/users/settings")[0] or {}
    except TraktError as e:
        if "HTTP 401" in str(e):
            _auth_fail(f"Trakt odmítl token ({e}).")
        print(f"Trakt nedostupný: {e}", file=sys.stderr)
        sys.exit(common.EXIT_ERROR)
    remaining = token_remaining() or 0
    print(f"Trakt OK ({(me.get('user') or {}).get('username') or '?'}), "
          f"token platí ještě {remaining / 86400:.1f} dne.")


def cmd_status(_args: argparse.Namespace) -> None:
    con = db()
    for table, label in (("live_movies", "filmy"), ("live_episodes", "epizody"),
                         ("ratings", "hodnocení"), ("watchlist", "watchlist"),
                         ("shows_progress", "seriály")):
        n = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"{label:12s} {n}")
    gone = sum(con.execute(f"SELECT COUNT(*) FROM {t} WHERE deleted_at IS NOT NULL").fetchone()[0]
               for t in ("watched_movies", "watched_episodes"))
    if gone:
        print(f"{'z Traktu smazané (archiv)':12s} {gone}")
    row = con.execute("SELECT value FROM meta WHERE key='synced_at'").fetchone()
    print(f"naposledy sync: {row[0] if row else 'nikdy'}")
    con.close()


def cmd_watchtime(args: argparse.Namespace) -> None:
    """Kolik času jsem u sledování strávil — co je v datech a jaký je odhad za život.

    Počítá se z `runtime` jednotlivých záznamů (Trakt ho dává u filmů i epizod).
    Filmy zatržené ručně v jednom termínu (stejná minuta) se hlásí zvlášť — v datech
    vypadají jako sledované ten den, ale skutečný čas sledování nezná.
    Odhad za život je jen počítadlo s parametry, které si zadáš: `--from-year`
    a `--hours-per-day`. Z dat se vzít nedá, pre-Trakt roky v žádném zdroji nejsou.
    """
    con = db()
    mv = list(con.execute("SELECT title, watched_at, runtime FROM live_movies"))
    ep = list(con.execute("SELECT show_title, watched_at, runtime FROM live_episodes"))
    tot_m = sum(r[2] or 0 for r in mv)
    tot_e = sum(r[2] or 0 for r in ep)
    tot = tot_m + tot_e

    def hm(m: int) -> str:
        return f"{m // 60} h {m % 60} min"

    def num(x: float, dec: int = 1) -> str:
        return f"{x:.{dec}f}".replace(".", ",")

    print("## Kolik času u sledování")
    print()
    print(f"- celkem **{hm(tot)}** — {num(tot / 1440)} dne, {num(tot / 1440 / 365.25, 2)} roku "
          "čistého času")
    print(f"- seriály: **{hm(tot_e)}** ({len(ep)} dílů) · filmy: **{hm(tot_m)}** "
          f"({len(mv)} zhlédnutí)")
    print()

    years: dict[str, int] = {}
    for _t, w, rt in mv + ep:
        p = dated(w)
        y = str(p.year) if p else "bez data"
        years[y] = years.get(y, 0) + (rt or 0)
    print("### Podle let")
    for y in sorted(years, key=lambda y: (y == "bez data", y)):
        share = 100 * years[y] / tot if tot else 0
        label = f"**{y}**" if y != "bez data" else "**bez data**"
        print(f"- {label}: {hm(years[y])} ({num(share, 0)} %)")
    print()

    hours: dict[str, int] = {}
    for t, _w, rt in mv + ep:
        hours[t] = hours.get(t, 0) + (rt or 0)
    print("### Nejvíc hodin podle titulu")
    for t, m in sorted(hours.items(), key=lambda kv: -kv[1])[:10]:
        print(f"- {t} — {hm(m)}")
    print()

    batches: dict[str, list[str]] = {}
    for t, w, _rt in mv:
        if (w or "") >= "2000":
            batches.setdefault(w[:16], []).append(t)
    batches = {k: v for k, v in batches.items() if len(v) > 1}
    n_batch = sum(len(v) for v in batches.values())
    if batches:
        print(f"### Filmy zatržené ručně, ne odsledované ({n_batch} z {len(mv)})")
        print()
        print("U těchto záznamů je datum nespolehlivé — vznikly označením „viděno“ "
              "v jedné chvíli, ne sledováním:")
        for k, v in sorted(batches.items()):
            print(f"- {k.replace('T', ' ')} — {', '.join(v)}")
        print()

    from_year = args.from_year
    per_day = args.hours_per_day
    now = dt.date.today()
    span_years = (now - dt.date(from_year, 1, 1)).days / 365.25
    est = span_years * 365.25 * per_day
    print(f"### Odhad za život (jen počítadlo, ne data)")
    print()
    print(f"Od roku {from_year} do dnes je {num(span_years)} roku. Při průměru "
          f"**{num(per_day)} h/den** to dělá **{est:.0f} h** = {est / 24:.0f} dní = "
          f"**{num(est / 24 / 365.25, 2)} roku** čistého času "
          f"({num(100 * est / (span_years * 365.25 * 24))} % z toho období).")
    print()
    print("Změň parametry, číslo se přepočítá: `track.py watchtime --from-year 2010 "
          "--hours-per-day 1.5`.")
    con.close()


def cmd_export(args: argparse.Namespace) -> None:
    """Kopie archivu mimo SQLite: CSV (movies.csv, episodes.csv) nebo jeden JSON.

    Výchozí jsou jen živé záznamy; `--include-deleted` přidá i to, co z Traktu
    zmizelo (sloupec deleted_at)."""
    import csv
    import io
    con = db()
    where = "" if args.include_deleted else " WHERE deleted_at IS NULL"
    tables = {}
    for name, table in (("movies", "watched_movies"), ("episodes", "watched_episodes")):
        cur = con.execute(f"SELECT * FROM {table}{where} ORDER BY watched_at")
        names = [d[0] for d in cur.description]
        keep = [i for i, c in enumerate(names) if args.include_deleted or c != "deleted_at"]
        tables[name] = ([names[i] for i in keep],
                        [tuple(r[i] for i in keep) for r in cur.fetchall()])
    con.close()
    out = pathlib.Path(args.out).expanduser()
    if args.format == "json":
        target = out / "trakt-export.json" if out.is_dir() else out
        data = {name: [dict(zip(cols, r)) for r in rows] for name, (cols, rows) in tables.items()}
        common.atomic_write_json(target, data, indent=1)
        print(f"Uloženo: {target} ({len(data['movies'])} filmů, {len(data['episodes'])} epizod)")
        return
    out.mkdir(parents=True, exist_ok=True)
    for name, (cols, rows) in tables.items():
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(cols)
        w.writerows(rows)
        common.atomic_write_text(out / f"{name}.csv", buf.getvalue())
        print(f"Uloženo: {out / f'{name}.csv'} ({len(rows)} řádků)")


def main() -> None:
    ap = argparse.ArgumentParser(description="Trakt tracker")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("auth", help="přihlášení přes device flow").set_defaults(func=cmd_auth)
    sub.add_parser("auth-check", help="ověří přihlášení (bez interakce, pro cron)").set_defaults(
        func=cmd_auth_check)
    sy = sub.add_parser("sync", help="stáhne data z Traktu do tracker.db")
    sy.add_argument("--allow-mass-delete", action="store_true",
                    help="dovolit označit jako smazané i velkou část historie")
    sy.set_defaults(func=cmd_sync)
    rp = sub.add_parser("report", help="vygeneruje přehled")
    rp.add_argument("--month", help="YYYY-MM (výchozí: tento měsíc)")
    rp.add_argument("--year", action="store_true", help="celý letošní rok")
    rp.add_argument("--all", action="store_true", help="celoživotní přehled")
    rp.set_defaults(func=cmd_report)
    sub.add_parser("status", help="stav databáze").set_defaults(func=cmd_status)
    wt = sub.add_parser("watchtime", help="kolik času u sledování (a odhad za život)")
    wt.add_argument("--from-year", type=int, default=2005, help="od kterého roku počítat odhad")
    wt.add_argument("--hours-per-day", type=float, default=2.0, help="průměr h/den do odhadu")
    wt.set_defaults(func=cmd_watchtime)
    ex = sub.add_parser("export", help="export archivu do CSV nebo JSON")
    ex.add_argument("--format", choices=("csv", "json"), default="csv")
    ex.add_argument("--out", required=True, help="cílová složka (CSV) nebo soubor/složka (JSON)")
    ex.add_argument("--include-deleted", action="store_true",
                    help="i záznamy, které z Traktu zmizely")
    ex.set_defaults(func=cmd_export)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
