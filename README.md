# stremio-trakt-track

Doplní Traktu, co se ve Stremiu zhlédlo, a vede lokální historii sledování v SQLite
(filmy, epizody, hodnocení, watchlist, postup u seriálů) včetně přehledů po měsících,
letech a „kolik času u toho" (`track.py watchtime`). Bez závislostí, jen stdlib Pythonu 3.

Vzniklo proto, že propojení Traktu přímo ve Stremiu se rozpadá (`stremio-bugs` #1436)
a historie se pak ztrácí. Most si stav Stremia přečte sám (jeho knihovnu a bitovou
mapu zhlédnutých dílů) a co v Traktu chybí, doplní — a umí i odebrat, co Stremio jako
zhlédnuté nevede.

Není to oficiální nástroj Stremia ani Traktu, obojí jsou cizí služby, ke kterým se
skript připojuje přes jejich API. Licence: MIT (`LICENSE`).

## Jednorázové nastavení

1. **Založit API aplikaci na Traktu:** https://trakt.tv/oauth/applications/new
   - Name: `stremio-trakt-track`
   - Redirect URI: `urn:ietf:wg:oauth:2.0:oob` — portál u něj ukáže žluté varování
     "Insecure redirect URIs". Je to jen varování; pokud aplikaci uloží, nech to tak.
     Kdyby uložení bloklo, použij jakoukoli HTTPS adresu, kterou vlastníš (URL se nikdy
     reálně nenavštěvuje, protože používáme device flow) a stejnou hodnotu zadej
     do setup skriptu.
   - Po uložení zkopíruj **Client ID** a **Client Secret**.
2. **Uložit údaje** (interaktivně, práva 600, hodnoty nejdou do historie shellu):
   ```bash
   bash ~/Documents/trakt-tracker/setup_secret.sh
   ```
3. **Přihlásit se device flow:**
   ```bash
   python3 ~/Documents/trakt-tracker/track.py auth
   ```
   Vypíše kód a URL `https://trakt.tv/activate`; kód se zadává na webu Traktu, ne do chatu.

## Běžné použití
```bash
python3 track.py sync              # stáhne vše z Traktu do tracker.db (idempotentní)
python3 track.py report            # přehled za aktuální měsíc
python3 track.py report --month 2026-09
python3 track.py report --year     # celý letošní rok
python3 track.py report --all      # celoživotní přehled (spočítaný z historie)
python3 track.py watchtime         # kolik času u sledování + odhad za život
python3 track.py status            # kolik je v DB záznamů + kdy proběhl sync
```

## Soubory

- `config.json` (600) – client_id, client_secret, access/refresh token. **Nikdy necommitovat.**
- `tracker.db` – SQLite: `watched_movies`, `watched_episodes`, `ratings`, `watchlist`,
  `shows_progress`, `meta` (username, stats, synced_at).
- `track.py` – CLI (`auth`, `sync`, `report`, `status`).
- `setup_secret.sh` – uložení API údajů.

## Poznámky k API

- Base URL `https://api.trakt.tv`, hlavičky `trakt-api-key`, `trakt-api-version: 2`, `User-Agent`.
- Limity: GET 1000 / 5 min, zápisy 1 / s. Kód řeší 429 přes `Retry-After`.
- Použité endpointy: `/oauth/device/code`, `/oauth/device/token`, `/oauth/token`,
  `/users/me`, `/users/me/stats`, `/users/me/watched/shows`, `/sync/history/{movies,episodes}`,
  `/sync/ratings/{movies,shows,seasons,episodes}`, `/sync/watchlist/{movies,shows}`,
  `/shows/{id}/progress/watched`.
- `sync` je read-only vůči Traktu – nic nezapisuje ani nemění.
- **`/users/me/stats` umí vrátit `null`** (Trakt statistiku nedá), takže
  celoživotní čísla se počítají z `tracker.db`: počty záznamů, unikátní tituly, hodiny
  z `runtime` jednotlivých záznamů, dny (`DISTINCT substr(watched_at,1,10)`) a roky.
- Historie začíná **6. 4. 2025** — starší sledování nikde není, Trakt ho nezná a Stremio
  si ho taky nepamatuje. Celoživotní přehled je tedy „od začátku účtu".

## Obnova tokenu

Access token platí ~90 dní. Když je v `config.json` i `client_secret`, `track.py` si token
obnoví sám. Bez secretu (když se přeskočil) je po vypršení potřeba spustit `auth` znovu.

## Most Stremio → Trakt

Stremio má dlouhodobou chybu: jeho zabudované Trakt propojení odpadá po hodinách až dnech
(GitHub Stremio/stremio-bugs#1436). Knihovna Stremia se ale synchronizuje spolehlivě přes
vlastní cloud, takže most čte zhlédnuté odtud a doplňuje, co v Traktu chybí.

```bash
bash setup_stremio.sh                    # jednorázově: přihlášení (uloží se jen authKey, 600)
python3 stremio_bridge.py fetch          # stáhne knihovnu → stremio_library.json
python3 stremio_bridge.py list           # co je ve knihovně a co je zhlédnuté
python3 stremio_bridge.py compare        # co má Stremio zhlédnuté a Trakt ne → gaps.json
python3 stremio_bridge.py push           # zápis chybějícího do Traktu (dry-run)
python3 stremio_bridge.py push --yes     # skutečný zápis
python3 stremio_bridge.py forward        # pro seriály s nečitelnou bitovou mapou (dry-run)
python3 stremio_bridge.py forward --yes  # doplní mezeru k poslednímu dokoukanému dílu
python3 stremio_bridge.py exact --only <imdb>        # srovná Trakt přesně na Stremiův seznam (dry-run)
python3 stremio_bridge.py exact --only <imdb> --yes  # i s odebráním dílů, které Stremio nevede
```

Jak to funguje:
- Stremio drží u seriálu bitovou mapu zhlédnutých epizod (`state.watched`, zlib+base64).
  Pole `<season>:<episode>` v ní je poslední **dokoukaný** díl (v mapě ještě není), zatímco
  `state.video_id` je poslední **přehrávaný** díl — ten může být rozehraný.
  Indexy se mapují na pořadí videí z Cinemeta (`cinemeta_cache.json`), které odpovídá
  řazení ve Stremiu (speciály S0 první, pak S1, S2…).
- U filmů se bere `timesWatched`/`flaggedWatched`/`lastWatched`.
- `push` posílá jen to, co v Traktu chybí (Trakt duplicity nehlídá), a zapisuje si, co už
  odeslal, do `pushed.json`.
- Přesné datum zhlédnutí zná Stremio jen u posledního dílu seriálu; starší epizody se
  zapisují jako `watched_at: "unknown"` — radši bez data než s vymyšleným. Trakt si je
  uloží s datem 1. 1. 1970, takže se nepočítají do statistik (`pochopeno` bolem).
- `python3 stremio_bridge.py redate --yes` tyhle epizody přepíše na den posledního dílu
  daného seriálu (smazat + zapsat znovu), aby statistiky seděly. Datum je odhad.
- `alias.json` řeší tituly, které má Trakt pod jiným IMDb ID než Stremio
  (např. seriál, který má v Traktu jiné IMDb ID než ve Stremiu).

### Automatizace (cron)

Denní a týdenní běh řeší dva skripty bez agenta (žádné LLM, jen deterministická práce):

- `cron_daily.py` — stáhne knihovnu Stremia, porovná s Traktem, zapíše chybějící filmy
  a epizody (`push`), doplní mezery k poslednímu dokoukanému dílu (`forward --max 8`)
  a zaktualizuje `tracker.db`. **Ticho znamená dobře** — vypíše se jen to, co se zapsalo,
  co potřebuje rozhodnutí (mezera nad 8 dílů), nebo co selhalo, takže prázdný výstup
  se nikam neposílá.
- `cron_weekly.py` — rekapitulace posledních sedmi dní z `tracker.db` (filmy, seriály,
  hodiny, kde jsme skončili) + souhrn za letošní rok.

Proč `--max 8`: když scrobblování vypadne na týden, mezera je pár dílů a automatika ji
doplní. Když je mezera velká (řádově desítky dílů), nejde o výpadek scrobblování, ale
o starší sledování — to se jen ohlásí a čeká na rozhodnutí, protože by to zkreslilo
statistiky i historii.

Ruční spuštění kdykoli:
```bash
python3 ~/Documents/trakt-tracker/cron_daily.py    # denní doplnění
python3 ~/Documents/trakt-tracker/cron_weekly.py   # týdenní přehled
```

## Záloha

`tracker.db` je lokální a do iCloudu se **nepřesouvá** — synchronizace na úrovni
souborů umí databázi rozbít (dva zápisy proti sobě, zámky, konfliktní kopie).
Místo toho se z ní dělá konzistentní snapshot a do iCloudu jde ten:

```sh
python3 backup_db.py            # snapshot + úklid starých
python3 backup_db.py --list     # co je v záloze
```

Snapshoty jdou do `~/Library/Mobile Documents/com~apple~CloudDocs/trakt-tracker-backups/`
(jde přenastavit přes `--to` nebo `TRAKT_BACKUP_DIR`), drží se všechny za posledních
30 dní plus nejnovější z každého měsíce. Kopie se po vytvoření kontroluje: musí projít
`PRAGMA integrity_check` a mít stejné počty záznamů jako živá databáze, jinak se zahodí.
Zálohuje se i `alias.json` (ruční opravy ID). Přihlašovací údaje se záměrně **ne**zálohují,
ty se dají znovu vytvořit setup skripty.

Bonus: snapshots jsou zároveň body návratu — když se z Traktu omylem smaže historie,
dá se záloha otevřít a zjistit, co tam bylo.

Co se zapsalo, si most zapisuje do `last_push.json` / `last_forward.json`; denní skript
z nich skládá zprávu. Porovnávat `pushed.json` před/po nestačí — když se záznam v Traktu
ztratí a doplní se znovu, v `pushed.json` už je a rozdíl vyjde prázdný.

## Na co pozor
- **`exact` = zrcadlo jednoho seriálu.** `push` jen doplňuje a `forward` doplní mezeru
  k poslednímu dokoukanému dílu — oba nechají v Traktu i díly, které uživatel přeskočil.
  `exact --only <imdb>` udělá z Traktu přesně to, co hlásí Stremio: doplní, co chybí,
  a odebere, co Stremio jako zhlédnuté nevede. Vždy nejdřív dry-run a pak zkontroluj
  `kontrola:` na konci — musí sedět počet i množina.
- **Cinemeta a Trakt číslují hodinové díly jinak.** Cinemeta je vede jako dva záznamy
  („Fun Run (1)“ / „(2)“), Trakt obvykle jako jeden — ale ne vždy: u některých seriálů je vede Trakt taky rozdělené. Proto se páry mapují podle názvů (přesná shoda včetně
  značky `(1)/(2)`, pak shoda bez značky) v rámci jedné řady a v pořadí; bez toho se
  oba půldíly slepí do jednoho dílu a jeden pak zbytečně zmizí (`map_cinemeta_pairs`).
- **`track.py sync` je zrcadlo, ne hromada.** Když se v Traktu něco smaže (ručně, přes
  `redate` nebo opravou), musí to zmizet i z `tracker.db` — jinak přehledy počítají mrtvé
  záznamy. Po opravách, které epizody přepisovaly, se takhle našlo 56 mrtvých epizod;
  přehled 2026 hlásil 252 epizod místo 245.
- **O tom, co chybí, rozhoduje živý stav Traktu, ne `pushed.json`.** Ten slouží jen pro
  `redate`; kdyby blokoval zápis, smazaný záznam by se už nikdy nedoplnil.
- **Mapování epizod není samozřejmost.** Bitová mapa Stremia je pořadí videí, jak je viděl
  Stremio; Cinemeta je dnes může řadit jinak. Každý seriál se proto ověřuje dvakrát:
  (1) nejvyšší zhlédnutý index musí odpovídat poli `season`/`episode`, které u sebe Stremio
  uvádí, (2) všechny namapované díly musí existovat v Traktu. Co neprojde, se **nezapisuje**
  a hlásí se zvlášť — jinak by do Traktu šly nesmysly typu `S0E110`.
- U části seriálů ověření neprochází a zůstávají ručně.
- Trakt historii **nededuplikuje** — každý zápis znamená další zhlédnutí. Proto se zapisuje
  jen to, co v Traktu chybí, a `pushed.json` drží přehled, co už bylo odesláno.
- Porovnání čte zhlédnuté filmy živě z Traktu, ne z lokální DB (ta je starší a hlásila by
  falešné rozdíly).

