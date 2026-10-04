# stremio-trakt-track

A one-way bridge from **Stremio to Trakt** plus a **local archive** of your Trakt
history in SQLite, with monthly/yearly/lifetime reports. Python 3 standard library
only — nothing to `pip install`.

Why: Stremio's built-in Trakt integration keeps disconnecting
([Stremio/stremio-bugs#1436](https://github.com/Stremio/stremio-bugs/issues/1436)),
and every disconnect silently loses watch history. Stremio's own library sync is
reliable, so the bridge reads what you watched from Stremio and fills in what Trakt
is missing.

This is a personal tool published as "works for me". It is not affiliated with
Stremio or Trakt. Licence: MIT (`LICENSE`). CLI output and reports are in Czech.

> **Stremio's API is unofficial and undocumented.** The bridge only *reads* from it
> (`api.strem.io` login, `datastoreGet`) and never writes to your Stremio account.
> It may break whenever Stremio changes it.

## Requirements

- Python ≥ 3.9 (uses `str.removeprefix`)
- SQLite ≥ 3.30 (`FILTER` clause; bundled with any recent Python)
- macOS or Linux (`strftime("%-d")` and `fcntl` are not available on Windows)
- A Trakt account and your own Trakt API application

## Setup

1. **Create a Trakt API app:** <https://trakt.tv/oauth/applications/new>.
   Any name; redirect URI `urn:ietf:wg:oauth:2.0:oob` (the portal warns that it is
   insecure — it is never visited, the tool uses the device flow). Copy the
   **Client ID** and **Client Secret**.
2. **Store the credentials** (interactive, file mode 600, never on the command line):
   ```bash
   bash setup_secret.sh
   ```
3. **Log in to Trakt** (device flow — you type the code on trakt.tv):
   ```bash
   python3 track.py auth
   ```
4. **Log in to Stremio** (only the `authKey` is stored, never the password):
   ```bash
   bash setup_stremio.sh
   ```
5. Optional: `cp alias.example.json alias.json` for titles that Trakt files under a
   different IMDb ID than Stremio.

All personal files (`config.json`, `stremio.json`, `tracker.db`, caches, journals,
`alias.json`) live next to the code and are git-ignored. Set `TRAKT_TRACKER_HOME`
to keep them elsewhere.

## Usage

### Local archive and reports (read-only towards Trakt)

```bash
python3 track.py sync                  # mirror Trakt history into tracker.db
python3 track.py report                # this month
python3 track.py report --month 2026-09
python3 track.py report --year         # this year
python3 track.py report --all          # lifetime, computed from history
python3 track.py watchtime             # time spent watching + a lifetime estimate
python3 track.py status                # row counts, last sync
python3 track.py export --format csv --out ~/trakt-export    # CSV (or --format json) copy
```

`sync` is a mirror, not a pile: entries deleted on Trakt are marked `deleted_at` and
drop out of the reports, but stay in the database. If a single sync would mark more
than 50 rows *and* more than 5 % of a table, it refuses (exit code 4) — that looks
like a Trakt outage, not a cleanup. Run `sync --allow-mass-delete` if it was intended.

### Bridge (writes to Trakt — dry-run unless `--yes`)

```bash
python3 stremio_bridge.py fetch              # download the Stremio library
python3 stremio_bridge.py list               # what Stremio has watched
python3 stremio_bridge.py compare            # Stremio vs live Trakt → gaps.json
python3 stremio_bridge.py push [--yes]       # add what Trakt is missing
python3 stremio_bridge.py forward [--yes]    # fill gaps for shows compare could not verify
python3 stremio_bridge.py exact --only <imdb> [--yes]   # make one show on Trakt match Stremio exactly
python3 stremio_bridge.py redate [--yes]     # give a date to episodes written as "unknown"
python3 stremio_bridge.py restore --from-journal last_exact.json [--yes]   # undo removals
```

Common flags: `--yes` (actually write), `--only <imdb>`, `--include-specials`,
`--json` (on `compare` and `forward`), `--force-repeat` (see below).

Exit codes: `0` ok, `1` error, `2` refused (e.g. unsafe `exact`), `3` another run
holds the lock, `4` sync prune guard, `5` login broken (needs a human), `10`
finished with warnings (unverified titles, rejected writes).

### Health check

```bash
python3 doctor.py               # everything, incl. Trakt/Stremio logins
python3 doctor.py --offline     # local state only
```

Checks the daily run watchdog, Trakt token (and that refreshing it works — by a real
call when there is no recent proof), Stremio session, library and unverified shows,
caches, journals and the lock, the backup age and the database. Every failed item
says which command fixes it. Exit code `0` ok, `10` warnings, `1` something broken.

## How it works

- Stremio keeps a per-show bitfield of watched episodes (`state.watched`,
  `<anchor video id>:<anchor length>:<base64(zlib(bits))>`, LSB-first). The anchor is
  the last watched video. Bits are mapped onto Cinemeta's video list and, like
  Stremio itself, re-anchored when that list changed since the bitfield was saved.
- A movie counts as watched when Stremio counted a play (`timesWatched`) or it was
  marked as watched — merely opening it is not enough.
- What Trakt already has is always read **live** (`/shows/{id}/progress/watched`
  per show, `/sync/history/movies`). The bulk `/sync/watched/shows` is not used: for
  some accounts it returns play counts without any episodes. An answer that looks
  valid but carries no data (e.g. `completed > 0` with no episodes) is treated as an
  error. If any read fails, the command aborts — it never
  proceeds with an empty set, because "Trakt has nothing" would mean writing
  everything again.
- Only the last watched episode has a real date in Stremio; older ones are written
  as `watched_at: unknown` (Trakt stores them as 1970-01-01 and leaves them out of
  statistics). `redate` can replace them with the date of the last episode.
- Cinemeta and Trakt sometimes number hour-long double episodes differently
  ("Title (1)"/"(2)" vs one episode). `exact` aligns whole seasons by title (by
  episode number when counts and numbers agree).

## Write safety

Trakt counts **every write as another play** and never deduplicates. The bridge
therefore:

- writes nothing without `--yes`, and lets only one writing run at a time (`.lock`);
- sends all writes through one module (`writes.py`) that appends every add and
  removal to `write_log.jsonl`, and **refuses to add the same item again within
  7 days** (reported as an anomaly — usually Trakt accepted the write but does not
  show it, e.g. a merged IMDb ID). `--force-repeat` overrides it;
- keeps per-run journals (`last_push.json`, `last_forward.json`, `last_exact.json`,
  `last_redate.json`) that are saved after every request.

`exact` and `redate` **remove** history. Both add first and remove second, remove
exact history entries by ID, and save the removed entries (with their original
`watched_at`) before deleting. `exact` refuses to run when an episode cannot be
paired or the target would be empty. Always look at the dry-run first.

`restore` puts removed entries back with their original `watched_at`, from a run
journal (`--from-journal`), from `write_log.jsonl` (`--from-log`, optionally
`--since`) or from a `tracker.db` backup snapshot (`--from-db`). Entries that are
already on Trakt (same title, episode and time) are skipped, so running it twice
does not create duplicates.

## Automation

`cron_daily.py` runs `track.py auth-check` → fetch → compare → push →
`forward --max 8` → `track.py sync` and prints only what was written and what needs
attention; silence means everything ran. A problem that was already reported and has
not changed is repeated only after `issue_remind_days` (3). Child steps run with
`TRAKT_TRACKER_NONINTERACTIVE=1` and no stdin, so nothing can wait for a password or a
device code. Transient network errors (5xx, timeouts) are retried for reads; writes
are never re-sent after an ambiguous error — the next run fills in whatever is still
missing from live data. After a fully successful run it writes `last_success.json`
and, if `settings.heartbeat_url` is set, pings it. `cron_weekly.py` prints a weekly recap. `backup_db.py` makes a
consistent `VACUUM INTO` snapshot of `tracker.db` (to iCloud Drive by default; see
`--to`, `TRAKT_BACKUP_DIR` or `settings.backup_dir`), keeps 30 days plus the latest
snapshot of each of the last 12 months, and verifies each snapshot.

Example crontab (mail the output, which is empty when nothing happened):

```cron
MAILTO=you@example.com
30 23 * * *  /usr/bin/python3 /path/to/stremio-trakt-track/cron_daily.py
45 23 * * *  /usr/bin/python3 /path/to/stremio-trakt-track/backup_db.py --quiet
0  20 * * 0  /usr/bin/python3 /path/to/stremio-trakt-track/cron_weekly.py
```

On macOS you can use a LaunchAgent instead — see `docs/launchd.example.plist`.
Running the daily job twice a day (e.g. 23:30 and 05:30) is safe — every run compares
live data and the lock prevents overlap — and covers a Trakt outage at night.

## When tracking stops

Silence from the daily job means it ran. If something breaks, the job says so; if the
job itself stops running, the weekly recap and the backup job report "Tracking
stojí od …" once the last successful run is older than 36 hours, and `doctor.py`
shows it too. Nothing on this machine can notice that *all* scheduled jobs stopped —
for that, set `settings.heartbeat_url` to a dead man's switch service (any URL that
alerts you when it is not called for a day, e.g. a self-hosted or hosted
"healthchecks" style check).

Start with `python3 doctor.py`. The five most likely causes:

1. **Trakt login expired or refresh failed** — report says "Obnovení Trakt tokenu
   selhalo" or "přihlášení nefunguje", exit code 5. Tokens last about a week and are
   refreshed a day ahead, so a broken refresh is reported while the old token still
   works. Fix: `python3 track.py auth` in a terminal (device flow: open the shown URL,
   type the code). Needs `client_secret` in `config.json` for automatic refresh.
2. **Stremio session gone** ("Session does not exist", "Stremio přihlášení neplatí").
   This cannot be repaired automatically because it needs your Stremio password.
   Until you fix it, nothing is filled in. Fix: `bash setup_stremio.sh`.
3. **A show cannot be verified** (listed under "Nesynchronizuje se"; its watched
   bitfield does not map onto Trakt's episodes, typically split double episodes).
   The job fills the gap up to the last watched episode automatically for up to 8
   missing episodes. Bigger gaps: look at `python3 stremio_bridge.py forward --only
   <imdb>` (dry-run), then add `--yes`; or make the show match exactly with
   `exact --only <imdb>`.
4. **Trakt or Stremio temporarily unavailable** ("nedostupné", HTTP 5xx, timeouts).
   Reads are retried; if it still fails, nothing is written that day and the next run
   catches up from live data. Fix: wait. Run `python3 cron_daily.py` by hand later if
   you do not want to wait for the next night.
5. **The scheduled job does not run at all** ("Tracking stojí od …" from the weekly or
   backup job, or `doctor.py`). Check that the scheduler still knows the job:
   `crontab -l` (cron) or `launchctl list | grep stremio` (launchd); look at the log
   files from the plist; run `python3 cron_daily.py` by hand and read its output. On
   macOS, cron may need "Full Disk Access" for `/usr/sbin/cron` if the code lives in
   Documents.

## Settings

Optional `"settings"` block in `config.json` (defaults shown):

```json
"settings": {
  "cache_ttl_hours": 24, "match_threshold": 0.6, "repeat_guard_days": 7,
  "max_auto": 8, "issue_remind_days": 3,
  "prune_max_rows": 50, "prune_max_fraction": 0.05, "backup_dir": null,
  "retry_delays": [2, 10, 30], "stale_hours": 36, "heartbeat_url": null
}
```

## Known limitations

- Specials (season 0) are skipped unless `--include-specials`.
- Only the last episode of a show gets a real watch date.
- Shows whose bitfield cannot be verified are handled by `forward`, which fills the
  gap up to the last watched episode (including episodes you may have skipped).
- Rewatches are not detected; a title already on Trakt is not written again.
- Trakt access tokens expire after about a week; with a client secret the tool
  refreshes them automatically, otherwise run `track.py auth` again.

## Tests

```bash
python3 -m unittest discover -s tests
```

The suite needs no network and never touches your data: it points
`TRAKT_TRACKER_HOME` at a temporary directory and fakes the Trakt API.

## Uninstall

1. Revoke the app on <https://trakt.tv/oauth/authorized_applications> and delete it
   under your API applications.
2. Delete `config.json` and `stremio.json`. The Stremio `authKey` stays valid until
   you log out of Stremio on all devices.
3. Remove cron jobs / LaunchAgents and, if you want, the backup folder.

## Notes

Operational history and the reasoning behind the design decisions (in Czech):
`docs/notes.md`.
