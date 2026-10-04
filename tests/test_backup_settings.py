"""Backup snapshot and retention, settings, atomic writes. Temp files only."""
import argparse
import datetime as dt
import json
import os
import sqlite3
import stat
import unittest
from unittest import mock

import helpers
from fakes import FakeTrakt, bitfield, network_error, write_json
import backup_db
import common
import stremio_bridge as b
import track


class Backup(unittest.TestCase):
    def setUp(self):
        helpers.clean_home()
        con = track.db()
        con.execute(f"INSERT INTO watched_movies({track.MOVIE_COLS}) "
                    "VALUES(1,1,'M',2000,'2026-01-01T10:00:00.000Z','watch','tt0000001',NULL,90)")
        con.commit()
        con.close()
        self.dst = helpers.TMP_HOME / "backups"

    def test_snapshot_is_complete_and_private(self):
        out = backup_db.make_snapshot(self.dst)
        con = sqlite3.connect(out)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM watched_movies").fetchone()[0], 1)
        con.close()
        self.assertEqual(stat.S_IMODE(os.stat(out).st_mode), 0o600)
        self.assertEqual(json.loads((self.dst / "last_backup.json").read_text())["db_records"], 1)
        self.assertEqual(list(self.dst.glob("*.tmp")), [])

    def test_retention_uses_name_date(self):
        self.dst.mkdir()
        today = dt.date(2026, 10, 4)
        names = ["tracker-2026-10-01.sqlite", "tracker-2026-09-20.sqlite",
                 "tracker-2026-08-10.sqlite", "tracker-2026-08-25.sqlite",
                 "tracker-2024-01-05.sqlite"]
        for n in names:
            (self.dst / n).write_text("x")
            os.utime(self.dst / n, (0, 0))          # mtime 1970 — must not matter
        removed = {p.name for p in backup_db.prune(self.dst, today=today)}
        # 30 dní + nejnovější z každého z posledních 12 měsíců, které mají zálohu
        self.assertEqual(removed, {"tracker-2026-08-10.sqlite"})

    def test_backup_dir_from_settings(self):
        write_json(helpers.TMP_HOME / "config.json", {"settings": {"backup_dir": "/tmp/somewhere"}})
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TRAKT_BACKUP_DIR", None)
            self.assertEqual(str(backup_db.target_dir(None)), "/tmp/somewhere")


class Settings(unittest.TestCase):
    def setUp(self):
        helpers.clean_home()

    def test_defaults_without_config(self):
        self.assertEqual(common.settings(), common.DEFAULTS)

    def test_override_and_unknown_keys_ignored(self):
        write_json(helpers.TMP_HOME / "config.json",
                   {"client_id": "x", "settings": {"max_auto": 3, "bogus": 1}})
        st = common.settings()
        self.assertEqual(st["max_auto"], 3)
        self.assertNotIn("bogus", st)

    def test_atomic_write_permissions(self):
        p = helpers.TMP_HOME / "secret.json"
        common.atomic_write_json(p, {"a": 1})
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
        self.assertEqual(json.loads(p.read_text()), {"a": 1})

    def test_cache_ttl_and_empty_values(self):
        c = common.JsonCache(helpers.TMP_HOME / "c.json", ttl_hours=1)
        c.put("a", [])
        self.assertIsNone(c.get("a"))
        c.put("b", [1])
        self.assertEqual(c.get("b"), [1])
        expired = common.JsonCache(helpers.TMP_HOME / "c.json", ttl_hours=0)
        self.assertIsNone(expired.get("b"))


class ForwardNetwork(unittest.TestCase):
    def setUp(self):
        helpers.clean_home()
        b._WATCHED.clear()

    def test_failed_read_in_forward_writes_nothing(self):
        sid = "tt0000001"
        write_json(b.RAW, [{"_id": sid, "type": "series", "name": "S",
                            "state": {"watched": bitfield(3, [0, 2], f"{sid}:1:3")}}])
        write_json(b.GAPS, {"movies": [], "shows": [], "unknown": [{"imdb": sid, "name": "S"}]})
        fake = FakeTrakt({("GET", f"/shows/{sid}/seasons"): [
            {"number": 1, "episodes": [{"number": e, "title": ""} for e in (1, 2, 3)]}],
            ("GET", f"/shows/{sid}/progress/watched"): network_error()})
        with mock.patch.object(track, "_req", fake):
            with self.assertRaises(track.TraktError):
                b.cmd_forward(argparse.Namespace(yes=True, only=None, date=None, max=None,
                                                 include_specials=False, force_repeat=False,
                                                 json=False))
        self.assertEqual(fake.posts(), [])


if __name__ == "__main__":
    unittest.main()
