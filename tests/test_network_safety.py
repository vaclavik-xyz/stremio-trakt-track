"""A failed read must never turn into a write. Trakt and Cinemeta are faked."""
import argparse
import json
import unittest
from unittest import mock

import helpers
from fakes import FakeTrakt, bitfield, history_ok, network_error, remove_ok, write_json
import stremio_bridge as b
import track
import writes

SID = "tt0000001"


def library(n_videos, watched_idx, anchor_ep):
    return [{"_id": SID, "type": "series", "name": "Show",
             "state": {"watched": bitfield(n_videos, watched_idx, f"{SID}:1:{anchor_ep}"),
                       "lastWatched": "2026-03-01T20:00:00.000Z",
                       "video_id": f"{SID}:1:{anchor_ep}"}}]


def cinemeta(titles):
    return [[f"{SID}:1:{e}", 1, e, t] for e, t in enumerate(titles, 1)]


def seasons(titles):
    return [{"number": 1, "episodes": [{"number": e, "title": t}
                                       for e, t in enumerate(titles, 1)]}]


def progress(pairs):
    """/shows/{id}/progress/watched answer with the given episodes completed."""
    seasons = {}
    for s, e in pairs:
        seasons.setdefault(s, []).append({"number": e, "completed": True})
    return {"aired": 10, "completed": len(pairs),
            "seasons": [{"number": s, "episodes": v} for s, v in sorted(seasons.items())]}


class Base(unittest.TestCase):
    def setUp(self):
        helpers.clean_home()
        b._WATCHED.clear()
        for target, attr, val in ((writes, "WRITE_SLEEP", 0),):
            p = mock.patch.object(target, attr, val)
            p.start()
            self.addCleanup(p.stop)

    def setup(self, lib, cm_titles, routes):
        write_json(b.RAW, lib)
        p = mock.patch.object(b, "cinemeta_videos", lambda sid, refresh=False: cinemeta(cm_titles))
        p.start()
        self.addCleanup(p.stop)
        fake = FakeTrakt(routes)
        p2 = mock.patch.object(track, "_req", fake)
        p2.start()
        self.addCleanup(p2.stop)
        return fake


class CompareAndPush(Base):
    TITLES = ["Pilot", "Arrival", "Heist"]

    def test_failed_episode_read_aborts_compare(self):
        fake = self.setup(library(3, [0, 1, 2], 3), self.TITLES, {
            ("GET", "/sync/history/movies"): [],
            ("GET", f"/shows/{SID}/seasons"): seasons(self.TITLES),
            ("GET", f"/shows/{SID}/progress/watched"): network_error(),
        })
        with self.assertRaises(track.TraktError):
            b.cmd_compare(argparse.Namespace())
        self.assertFalse(b.GAPS.exists(), "no gaps.json from incomplete data")
        self.assertEqual(fake.posts(), [])

    def test_failed_movie_read_has_no_db_fallback(self):
        self.setup([], [], {("GET", "/sync/history/movies"): network_error()})
        with self.assertRaises(track.TraktError):
            b.compute_gaps()
        self.assertFalse((helpers.TMP_HOME / "tracker.db").exists())

    def test_failed_inventory_read_aborts_compare(self):
        self.setup(library(3, [0, 1, 2], 3), self.TITLES, {
            ("GET", "/sync/history/movies"): [],
            ("GET", f"/shows/{SID}/seasons"): network_error(),
        })
        with self.assertRaises(track.TraktError):
            b.compute_gaps()

    def test_push_twice_when_trakt_does_not_show_it(self):
        """H2: Trakt accepts the write but the read endpoint keeps missing it."""
        fake = self.setup(library(3, [0, 1, 2], 3), self.TITLES, {
            ("GET", "/sync/history/movies"): [],
            ("GET", f"/shows/{SID}/seasons"): seasons(self.TITLES),
            ("GET", f"/shows/{SID}/progress/watched"): progress([(1, 1)]),
            ("POST", "/sync/history"): history_ok,
        })
        args = argparse.Namespace(yes=True, include_specials=False, force_repeat=False)
        b.cmd_compare(argparse.Namespace())
        b.cmd_push(args)
        self.assertEqual(len(fake.posts()), 1)
        journal = json.loads(b.LAST_PUSH.read_text())
        self.assertEqual(journal["shows"][0]["pairs"], [[1, 2], [1, 3]])

        b._WATCHED.clear()
        b.cmd_compare(argparse.Namespace())
        b.cmd_push(args)
        self.assertEqual(len(fake.posts()), 1, "second night must not write again")
        journal = json.loads(b.LAST_PUSH.read_text())
        self.assertEqual(len(journal["repeat"]), 2)


class ShapeGuard(Base):
    """A valid-looking answer without the data we need must fail, not mean "nothing"."""
    TITLES = ["Pilot", "Arrival", "Heist"]

    def test_progress_without_seasons_aborts(self):
        # takhle vypadal skutečný problém: souhrn bez epizod (plays/completed, žádné seasons)
        fake = self.setup(library(3, [0, 1, 2], 3), self.TITLES, {
            ("GET", "/sync/history/movies"): [],
            ("GET", f"/shows/{SID}/seasons"): seasons(self.TITLES),
            ("GET", f"/shows/{SID}/progress/watched"): {"aired": 3, "completed": 3,
                                                        "last_watched_at": "2026-01-01T00:00:00.000Z"},
        })
        with self.assertRaises(track.TraktError):
            b.cmd_compare(argparse.Namespace())
        self.assertFalse(b.GAPS.exists())
        self.assertEqual(fake.posts(), [])

    def test_progress_with_nothing_watched_is_fine(self):
        self.setup([], [], {})
        with mock.patch.object(track, "_req", FakeTrakt({
                ("GET", f"/shows/{SID}/progress/watched"): {"aired": 3, "completed": 0, "seasons": []}})):
            self.assertEqual(b.trakt_watched_episodes(SID), set())

    def test_movie_history_without_ids_aborts(self):
        self.setup([], [], {("GET", "/sync/history/movies"): [{"id": 1, "movie": {"title": "X"}}]})
        with self.assertRaises(track.TraktError):
            b.compute_gaps()

    def test_seasons_without_episodes_abort(self):
        self.setup([], [], {("GET", f"/shows/{SID}/seasons"): [{"number": 1}, {"number": 2}]})
        with self.assertRaises(track.TraktError):
            b.trakt_seasons(SID)


class Exact(Base):
    def args(self, **kw):
        base = dict(only=SID, yes=True, date=None, include_specials=False,
                    allow_unmatched=False, force_repeat=False)
        base.update(kw)
        return argparse.Namespace(**base)

    def test_failed_title_read_writes_nothing(self):
        fake = self.setup(library(3, [0, 2], 3), ["Pilot", "Arrival", "Heist"], {
            ("GET", f"/shows/{SID}/seasons"): network_error(),
        })
        with self.assertRaises(track.TraktError):
            b.cmd_exact(self.args())
        self.assertEqual(fake.posts(), [])

    def test_unmatched_refuses(self):
        fake = self.setup(library(4, [0, 3], 4), ["Pilot", "Arrival", "Heist", "Bonus"], {
            ("GET", f"/shows/{SID}/seasons"): seasons(["Pilot", "Arrival", "Heist"]),
            ("GET", f"/shows/{SID}/progress/watched"): progress([(1, 1), (1, 2), (1, 3)]),
        })
        with self.assertRaises(SystemExit) as cm:
            b.cmd_exact(self.args())
        self.assertEqual(cm.exception.code, 2)
        self.assertEqual(fake.posts(), [])

    def test_add_before_remove_and_journal_first(self):
        titles = ["Pilot", "Arrival", "Heist"]
        state = {"pairs": [(1, 1), (1, 2)]}

        def on_add(body, params):
            state["pairs"].append((1, 3))
            return history_ok(body, params)

        def on_remove(body, params):
            journal = json.loads(b.LAST_EXACT.read_text())
            self.assertEqual([e["history_id"] for e in journal["remove_entries"]], [502])
            state["pairs"].remove((1, 2))
            return remove_ok(body, params)

        fake = self.setup(library(3, [0, 2], 3), titles, {
            ("GET", f"/shows/{SID}/seasons"): seasons(titles),
            ("GET", f"/shows/{SID}/progress/watched"): lambda body, params: progress(state["pairs"]),
            ("GET", f"/sync/history/shows/{SID}"): [
                {"id": 501, "type": "episode", "watched_at": "2026-01-01T20:00:00.000Z",
                 "episode": {"season": 1, "number": 1}, "show": {"title": "Show", "ids": {"imdb": SID}}},
                {"id": 502, "type": "episode", "watched_at": "2026-01-02T20:00:00.000Z",
                 "episode": {"season": 1, "number": 2}, "show": {"title": "Show", "ids": {"imdb": SID}}},
            ],
            ("POST", "/sync/history"): on_add,
            ("POST", "/sync/history/remove"): on_remove,
        })
        b.cmd_exact(self.args())
        self.assertEqual([p for p, _ in fake.posts()], ["/sync/history", "/sync/history/remove"])
        self.assertEqual(fake.posts()[1][1], {"ids": [502]})
        journal = json.loads(b.LAST_EXACT.read_text())
        self.assertEqual(journal["status"], "done")
        removed = [r for r in writes.log_records() if r["op"] == "remove"]
        self.assertEqual(removed[0]["watched_at"], "2026-01-02T20:00:00.000Z")


if __name__ == "__main__":
    unittest.main()
