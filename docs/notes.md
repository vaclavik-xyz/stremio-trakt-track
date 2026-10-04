# Provozní poznámky a rozhodnutí

Historie toho, proč je most postavený tak, jak je. README popisuje použití; tady
je „proč“. Konkrétní tituly jsou vynechané záměrně.

## Proč most vůbec existuje

Zabudované Trakt propojení ve Stremiu odpadá po hodinách až dnech
(Stremio/stremio-bugs#1436). Knihovna Stremia se ale synchronizuje spolehlivě přes
vlastní cloud, takže most čte zhlédnuté odtud a doplňuje, co v Traktu chybí.

## Trakt nededuplikuje

Každý zápis do historie je pro Trakt další zhlédnutí. Z toho plyne většina pojistek:

- o tom, co chybí, rozhoduje vždy **živý** stav Traktu, ne lokální DB ani
  `pushed.json` — kdyby `pushed.json` blokoval zápis, smazaný záznam by se už nikdy
  nedoplnil;
- když se čtení z Traktu nepovede, příkaz skončí chybou. Dřív se chyba měnila na
  prázdnou množinu a výpadek sítě znamenal zapsat celý seriál znovu;
- `cron_daily.py` maže `gaps.json` na začátku běhu a při selhání `fetch`/`compare`
  přeskočí `push` i `forward`;
- `write_log.jsonl` + pojistka proti opakování: když Trakt zápis přijme, ale čtecí
  endpoint ho nevidí (skrytá řada v `progress/watched`, sloučené IMDb ID), dřív se
  totéž zapisovalo každou noc. Teď se to po prvním zápisu jen ohlásí;
- `/sync/watched/shows` se **nepoužívá**: u některých účtů vrací jen `plays`
  a `last_watched_at`, bez `seasons` (ověřeno syrovým HTTP, i s `extended=full`).
  Krátce byl nasazený a `compare` pak chtěl doplnit 302 dílů, které v Traktu už
  byly — čtení „neselhalo“, takže gate v cronu nic nepoznal. Teď se čte
  `/shows/{id}/progress/watched` po seriálech a odpověď, která vypadá platně, ale
  nemá data (`completed > 0` bez dílů, filmy bez IMDb ID, řady bez dílů), je chyba.
  Díly ve skrytých řadách v progress chybí; opakovanému zápisu brání pojistka.
- Bity bitové mapy za kotvou (index ≥ `anchor_length`) se nepočítají. V živých
  datech existují i zjevně nesmyslné (kotva S0E1, nastavený bit 199); jen se hlásí.

## Bitová mapa Stremia

`state.watched` = `<anchor_video_id>:<anchor_length>:<base64(zlib(bitset))>`, bity
LSB-first. Kotva je video s nejvyšším nastaveným bitem (poslední dokoukaný díl),
`anchor_length` = jeho index + 1 (stremio-core, `WatchedBitField`). Pole
`state.video_id` je naproti tomu poslední *přehrávaný* díl, může být rozehraný.

Stremio při načtení mapu zarovná podle kotvy: když se seznam videí od uložení změnil
(Cinemeta přidala speciál nebo díl), bity posune o `n - 1 - nový_index_kotvy`.
První verze mostu to nedělala a místo toho vyžadovala shodu délky a markeru —
u několika seriálů ověření kvůli tomu neprocházelo a zůstávaly ruční. Teď most
zarovnává stejně jako Stremio; kotva, která v Cinemetě není, = neověřeno.

Mapování se dál kontroluje: všechny namapované díly musí existovat v Traktu, jinak
by do Traktu šly nesmysly typu `S0E110`.

## Dvojdíly a párování

Cinemeta vede hodinové díly často jako dva záznamy („Název (1)“ / „(2)“), Trakt
obvykle jako jeden — ale ne vždy, u jedné řady jednoho sitcomu je Trakt vede taky
rozdělené. První verze párovala v okně čtyř dílů a u generických názvů („Episode 10“
vs „Episode 3“, podobnost 0,84) nebo po mezeře v sledování párovala špatně. `exact`
pak špatně spárovaný díl z Traktu smazal. Teď se zarovnávají celé řady (podle čísla,
když sedí počty, jinak DP přes názvy) a `exact` při jakémkoli nenamapovaném dílu
nic nemění.

## `--max 8` v cronu

Když scrobblování vypadne na týden, mezera je pár dílů a automatika ji doplní. Když
je mezera velká (u jednoho dlouhého sitcomu 149 dílů), nejde o výpadek scrobblování,
ale o starší sledování — to se jen ohlásí a čeká na rozhodnutí, protože by to
zkreslilo statistiky i historii.

## `sync` je zrcadlo, ne hromada — ale s archivem

Když se v Traktu něco smaže (ručně, přes `redate` nebo opravou), nesmí se to počítat
v přehledech. Po opravách, které epizody přepisovaly, se takhle jednou našlo přes 50
mrtvých epizod a roční přehled byl o ~3 % nadsazený. Původně se řádky mazaly; teď se
jen označí `deleted_at` (archiv má historii chránit, ne zrcadlit její ztrátu)
a hromadné zmizení nad prahem se odmítne jako pravděpodobný výpadek Traktu.

## Data a časová pásma

Trakt ukládá `watched_at` v UTC (`…Z`). Hranice přehledů se převádějí do stejného
tvaru, jinak se noční sledování mezi 0–2 h přesouvalo do předchozího dne/měsíce.
Dny a roky se počítají podle místního data.

`/users/me/stats` umí vrátit `null`, proto se celoživotní čísla počítají z historie.
Trakt vede neznámé datum jako 1. 1. 1970 — přehledy ho počítají zvlášť jako „bez
data“. Filmy zatržené ručně v jedné minutě (batch) `watchtime` hlásí odděleně, protože
datum u nich neodpovídá sledování.

## Záloha

`tracker.db` se do iCloudu nepřesouvá — synchronizace na úrovni souborů umí živou
SQLite databázi rozbít. Do iCloudu jde konzistentní snapshot (`VACUUM INTO`),
ověřený `integrity_check` a počty řádků.

Na jednom stroji se ukázalo, že iCloud umí být mrtvý tak, že visí i výpis složky
(`InterruptedError: [Errno 4]`, služba `bird` nefunguje) — záloha do iCloudu tím
nikdy nevznikla a databáze neměla žádnou zálohu. Proto je primární **lokální**
snímek v `backups/` (vznikne za zlomek sekundy) a kopie mimo stroj běží
v odděleném procesu s tvrdým limitem: vlákno zaseknuté v jádře nejde ukončit,
proces jde zabít a opustit. Nedostupný cíl = varování, ne selhání jobu.
