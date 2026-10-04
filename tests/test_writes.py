"""The write layer: repeat guard, write log, partial results. Trakt is faked."""
import unittest
from unittest import mock

import helpers
from fakes import FakeTrakt, history_ok, remove_ok
import track
import writes

SHOW = {"imdb": "tt0000001"}


class WriteLayer(unittest.TestCase):
    def setUp(self):
        helpers.clean_home()
        p = mock.patch.object(writes, "WRITE_SLEEP", 0)
        p.start()
        self.addCleanup(p.stop)

    def fake(self, routes):
        fake = FakeTrakt(routes)
        p = mock.patch.object(track, "_req", fake)
        p.start()
        self.addCleanup(p.stop)
        return fake

    def test_add_logs_and_guard_blocks_repeat(self):
        fake = self.fake({("POST", "/sync/history"): history_ok})
        items = [writes.episode_item(SHOW, 1, 1, None, "Show"),
                 writes.episode_item(SHOW, 1, 2, "2026-01-02T20:00:00.000Z", "Show")]
        res = writes.add_history(items, cmd="test")
        self.assertEqual(len(res["added"]), 2)
        self.assertEqual(len(fake.posts()), 1)
        body = fake.posts()[0][1]
        eps = body["shows"][0]["seasons"][0]["episodes"]
        self.assertEqual(eps[0]["watched_at"], "unknown")
        self.assertEqual(len(writes.log_records()), 2)

        res2 = writes.add_history(items, cmd="test")
        self.assertEqual(res2["added"], [])
        self.assertEqual(len(res2["repeat"]), 2)
        self.assertEqual(len(fake.posts()), 1, "repeat must not reach Trakt")

        res3 = writes.add_history(items[:1], cmd="test", force_repeat=True)
        self.assertEqual(len(res3["added"]), 1)

    def test_guard_window_expires(self):
        self.fake({("POST", "/sync/history"): history_ok})
        item = writes.movie_item({"imdb": "tt0000002"}, None, "Movie")
        writes.add_history([item], cmd="test")
        res = writes.add_history([item], cmd="test", guard_days=0)
        self.assertEqual(len(res["added"]), 1)

    def test_remove_then_add_is_allowed(self):
        self.fake({("POST", "/sync/history"): history_ok,
                   ("POST", "/sync/history/remove"): remove_ok})
        item = writes.episode_item(SHOW, 1, 1)
        writes.add_history([item], cmd="test")
        entry = {**item, "history_id": 99, "watched_at": "2026-01-01T10:00:00.000Z"}
        rres = writes.remove_history([entry], cmd="test")
        self.assertEqual(len(rres["removed"]), 1)
        rec = writes.log_records()[-1]
        self.assertEqual((rec["op"], rec["history_id"], rec["watched_at"]),
                         ("remove", 99, "2026-01-01T10:00:00.000Z"))
        res = writes.add_history([item], cmd="test")
        self.assertEqual(len(res["added"]), 1)

    def test_duplicates_in_one_call_sent_once(self):
        fake = self.fake({("POST", "/sync/history"): history_ok})
        item = writes.episode_item(SHOW, 1, 1)
        writes.add_history([item, dict(item)], cmd="test")
        eps = fake.posts()[0][1]["shows"][0]["seasons"][0]["episodes"]
        self.assertEqual(len(eps), 1)

    def test_partial_accept_is_reported(self):
        self.fake({("POST", "/sync/history"):
                   {"added": {"episodes": 1}, "not_found": {}}})
        items = [writes.episode_item(SHOW, 1, e, None, "Show") for e in (1, 2)]
        res = writes.add_history(items, cmd="test")
        self.assertEqual(res["added"], [])
        self.assertEqual(len(res["uncertain"]), 2)
        self.assertIn("accepted 1 of 2", res["denied"][0])
        # logged anyway, so the guard errs on the side of no duplicates
        self.assertEqual(len(writes.recent_adds(7)), 2)

    def test_movie_not_found(self):
        self.fake({("POST", "/sync/history"):
                   {"added": {"movies": 1},
                    "not_found": {"movies": [{"ids": {"imdb": "tt0000003"}}]}}})
        items = [writes.movie_item({"imdb": "tt0000002"}, None, "A"),
                 writes.movie_item({"imdb": "tt0000003"}, None, "B")]
        res = writes.add_history(items, cmd="test")
        self.assertEqual([i["name"] for i in res["added"]], ["A"])
        self.assertEqual([i["name"] for i in res["not_found"]], ["B"])

    def test_network_error_propagates_after_progress(self):
        calls = []
        n = {"i": 0}

        def flaky(body, params):
            n["i"] += 1
            if n["i"] == 2:
                raise track.TraktError("timed out")
            return history_ok(body, params)

        self.fake({("POST", "/sync/history"): flaky})
        items = [writes.episode_item({"imdb": "tt0000001"}, 1, 1),
                 writes.episode_item({"imdb": "tt0000009"}, 1, 1)]
        with self.assertRaises(track.TraktError):
            writes.add_history(items, cmd="test", on_progress=lambda r: calls.append(len(r["added"])))
        self.assertEqual(calls, [1], "progress saved for the first show before the failure")


if __name__ == "__main__":
    unittest.main()
