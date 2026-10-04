"""tracker.db: soft delete, prune guard, report period boundaries. Temp DB only."""
import argparse
import contextlib
import datetime as dt
import io
import unittest
from unittest import mock

import helpers
from fakes import FakeTrakt
import common
import track


def movie_row(hid, when="2026-01-10T20:00:00.000Z"):
    return {"id": hid, "watched_at": when, "action": "watch",
            "movie": {"title": f"Movie {hid}", "year": 2000, "runtime": 100,
                      "ids": {"trakt": hid, "imdb": f"tt{hid:07d}"}}}


def ep_row(hid, when="2026-01-10T20:00:00.000Z"):
    return {"id": hid, "watched_at": when, "action": "watch",
            "show": {"title": "Show", "ids": {"trakt": 1}},
            "episode": {"season": 1, "number": hid, "title": f"E{hid}", "runtime": 30}}


def routes(movies, eps):
    r = {("GET", "/users/me"): {"username": "tester"},
         ("GET", "/sync/history/movies"): movies,
         ("GET", "/sync/history/episodes"): eps,
         ("GET", "/users/me/watched/shows"): [],
         ("GET", "/users/me/stats"): None}
    for kind in ("movies", "shows", "seasons", "episodes"):
        r[("GET", f"/sync/ratings/{kind}")] = []
    for kind in ("movies", "shows"):
        r[("GET", f"/sync/watchlist/{kind}")] = []
    return r


class Sync(unittest.TestCase):
    def setUp(self):
        helpers.clean_home()

    def sync(self, movies, eps, **kw):
        with mock.patch.object(track, "_req", FakeTrakt(routes(movies, eps))), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            track.cmd_sync(argparse.Namespace(allow_mass_delete=kw.get("allow", False)))
        return err.getvalue()

    def counts(self):
        con = track.db()
        try:
            return (con.execute("SELECT COUNT(*) FROM live_movies").fetchone()[0],
                    con.execute("SELECT COUNT(*) FROM watched_movies").fetchone()[0])
        finally:
            con.close()

    def test_removed_entry_is_soft_deleted_and_revives(self):
        self.sync([movie_row(i) for i in range(1, 11)], [])
        self.sync([movie_row(i) for i in range(1, 10)], [])
        self.assertEqual(self.counts(), (9, 10))
        self.sync([movie_row(i) for i in range(1, 11)], [])
        self.assertEqual(self.counts(), (10, 10))

    def test_mass_disappearance_is_refused(self):
        self.sync([movie_row(i) for i in range(1, 201)], [])
        with self.assertRaises(SystemExit) as cm:
            self.sync([], [])
        self.assertEqual(cm.exception.code, common.EXIT_GUARD)
        self.assertEqual(self.counts(), (200, 200), "nothing marked deleted")
        self.sync([], [], allow=True)
        self.assertEqual(self.counts(), (0, 200), "archive keeps the rows")

    def test_migration_adds_column_to_old_db(self):
        import sqlite3
        con = sqlite3.connect(track.DB)
        con.execute("CREATE TABLE watched_movies(history_id INTEGER PRIMARY KEY, trakt_id INTEGER, "
                    "title TEXT, year INTEGER, watched_at TEXT, action TEXT, imdb TEXT, tmdb TEXT, "
                    "runtime INTEGER)")
        con.execute("INSERT INTO watched_movies VALUES(1,1,'Old',2000,'2025-05-01T10:00:00.000Z',"
                    "'watch','tt0000001',NULL,90)")
        con.commit()
        con.close()
        self.assertEqual(self.counts(), (1, 1))


class ReportBounds(unittest.TestCase):
    def setUp(self):
        helpers.clean_home()

    def test_utc_bound_shape(self):
        cest = dt.timezone(dt.timedelta(hours=2))
        self.assertEqual(common.utc_bound(dt.datetime(2026, 10, 1, tzinfo=cest)),
                         "2026-09-30T22:00:00")

    def test_late_night_episode_lands_in_local_month(self):
        cest = dt.timezone(dt.timedelta(hours=2))
        start = common.utc_bound(dt.datetime(2026, 10, 1, tzinfo=cest))
        end = common.utc_bound(dt.datetime(2026, 11, 1, tzinfo=cest))
        watched = "2026-09-30T22:30:00.000Z"          # 1. 10. 0:30 místního času
        self.assertTrue(start <= watched < end)
        self.assertFalse(watched < start)
        boundary = "2026-09-30T22:00:00.000Z"          # přesně půlnoc → patří do října
        self.assertTrue(start <= boundary)

    def test_monthly_report_uses_local_month(self):
        import os
        import time
        old_tz = os.environ.get("TZ")
        os.environ["TZ"] = "Europe/Prague"
        time.tzset()
        try:
            con = track.db()
            con.execute(f"INSERT INTO watched_episodes({track.EPISODE_COLS}) "
                        "VALUES(1,1,'Show',1,1,'E1','2026-09-30T22:30:00.000Z','watch',30)")
            con.execute(f"INSERT INTO watched_episodes({track.EPISODE_COLS}) "
                        "VALUES(2,1,'Show',1,2,'E2','2026-09-30T21:30:00.000Z','watch',30)")
            con.commit()
            con.close()
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                track.cmd_report(argparse.Namespace(all=False, year=False, month="2026-10"))
            self.assertIn("1 epizoda", out.getvalue())
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                track.cmd_report(argparse.Namespace(all=False, year=False, month="2026-09"))
            self.assertIn("1 epizoda", out.getvalue())
        finally:
            if old_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old_tz
            time.tzset()

    def test_dated_ignores_unknown(self):
        self.assertIsNone(track.dated("1970-01-01T00:00:00.000Z"))
        self.assertIsNone(track.dated(None))
        self.assertIsNotNone(track.dated("2026-01-01T00:00:00.000Z"))


class Formatting(unittest.TestCase):
    def test_cn(self):
        self.assertEqual(common.cn(1, "film", "filmy", "filmů"), "1 film")
        self.assertEqual(common.cn(3, "film", "filmy", "filmů"), "3 filmy")
        self.assertEqual(common.cn(0, "film", "filmy", "filmů"), "0 filmů")
        self.assertEqual(common.cn(5, "film", "filmy", "filmů"), "5 filmů")

    def test_fmt_eps(self):
        self.assertEqual(common.fmt_eps([(2, 3), (2, 1), (2, 2), (3, 1), (2, 5)]),
                         "S2E1–E3, S2E5, S3E1")
        self.assertEqual(common.fmt_eps([(0, 1), (0, 2)]), "speciály E1–E2")


if __name__ == "__main__":
    unittest.main()
