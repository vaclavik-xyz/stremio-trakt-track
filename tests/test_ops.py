"""Operational safety: lock, cron issue reminders, forward/redate scope, compare per item."""
import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

import helpers
from fakes import FakeTrakt, bitfield, history_ok, remove_ok, write_json
import common
import cron_daily
import stremio_bridge as b
import track
import writes

SID = "tt0000001"


class Lock(unittest.TestCase):
    def setUp(self):
        helpers.clean_home()

    def child(self, env_extra=None, pass_fds=()):
        code = ("import sys; sys.path.insert(0, %r); import common\n"
                "with common.lock(): print('locked')\n") % str(helpers.CODE_DIR)
        env = {**os.environ, **(env_extra or {})}
        return subprocess.run([sys.executable, "-c", code], env=env, pass_fds=pass_fds,
                              capture_output=True, text=True, timeout=30)

    def test_second_process_is_refused(self):
        with common.lock():
            p = self.child()
        self.assertEqual(p.returncode, common.EXIT_LOCKED)
        self.assertEqual(self.child().returncode, 0)

    def test_child_inherits_parent_lock(self):
        with common.lock() as fd:
            p = self.child({common.LOCK_ENV: str(fd)}, pass_fds=(fd,))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("locked", p.stdout)


class IssueReminders(unittest.TestCase):
    def setUp(self):
        helpers.clean_home()

    def test_new_then_every_n_days(self):
        day = 86400
        t0 = 1_800_000_000
        cur = {"unknown|tt1": "Show: neověřeno"}
        self.assertEqual(len(cron_daily.issues_to_report(cur, now=t0, remind_days=7)), 1)
        self.assertEqual(cron_daily.issues_to_report(cur, now=t0 + day, remind_days=7), [])
        self.assertIn("trvá 7 dní", cron_daily.issues_to_report(cur, now=t0 + 7 * day, remind_days=7)[0])
        self.assertEqual(cron_daily.issues_to_report({}, now=t0 + 8 * day, remind_days=7), [])
        self.assertIn("nové", cron_daily.issues_to_report(cur, now=t0 + 9 * day, remind_days=7)[0])


def series(sid, n, bits, anchor_ep, name="Show"):
    return {"_id": sid, "type": "series", "name": name,
            "state": {"watched": bitfield(n, bits, f"{sid}:1:{anchor_ep}"),
                      "lastWatched": "2026-03-01T20:00:00.000Z"}}


class Scoped(unittest.TestCase):
    def setUp(self):
        helpers.clean_home()
        b._WATCHED.clear()
        for target, attr, val in ((writes, "WRITE_SLEEP", 0),):
            p = mock.patch.object(target, attr, val)
            p.start()
            self.addCleanup(p.stop)

    def fake(self, routes):
        fake = FakeTrakt(routes)
        p = mock.patch.object(track, "_req", fake)
        p.start()
        self.addCleanup(p.stop)
        return fake

    def test_compare_survives_one_bad_item(self):
        lib = [{"_id": SID, "type": "series", "name": "Broken", "state": {"watched": "garbage"}},
               {"_id": "tt0000002", "type": "movie", "name": "Good", "state": {"timesWatched": 1}}]
        write_json(b.RAW, lib)
        self.fake({("GET", "/sync/history/movies"): []})
        gaps = b.compute_gaps()
        self.assertEqual([u["name"] for u in gaps["unknown"]], ["Broken"])
        self.assertEqual([m["name"] for m in gaps["movies"]], ["Good"])

    def test_forward_only_touches_unknown_shows(self):
        lib = [series(SID, 4, [0, 3], 4, "Unverified"), series("tt0000002", 4, [0, 3], 4, "Verified")]
        write_json(b.RAW, lib)
        write_json(b.GAPS, {"movies": [], "shows": [],
                            "unknown": [{"imdb": SID, "name": "Unverified", "reason": "x"}]})
        seasons = [{"number": 1, "episodes": [{"number": e, "title": f"E{e}"} for e in range(1, 5)]}]
        fake = self.fake({
            ("GET", f"/shows/{SID}/seasons"): seasons,
            ("GET", f"/shows/{SID}/progress/watched"): {"aired": 4, "completed": 0, "seasons": []},
            ("POST", "/sync/history"): history_ok,
        })
        args = argparse.Namespace(yes=True, only=None, date=None, max=None, include_specials=False,
                                  force_repeat=False, json=False)
        with contextlib.redirect_stdout(io.StringIO()):
            rc = b.cmd_forward(args)
        self.assertEqual(rc, common.EXIT_OK)
        shows = [p[1]["shows"][0]["ids"]["imdb"] for p in fake.posts()]
        self.assertEqual(shows, [SID])

    def test_forward_refuses_without_fresh_gaps(self):
        write_json(b.RAW, [])
        with self.assertRaises(SystemExit):
            b.cmd_forward(argparse.Namespace(yes=False, only=None, date=None, max=None,
                                             include_specials=False, force_repeat=False, json=False))

    def test_redate_only_touches_undated_entries(self):
        write_json(b.RAW, [series(SID, 2, [0, 1], 2)])
        write_json(b.PUSHED, {"movies": [], "episodes": [[SID, 1, 1], [SID, 1, 1], [SID, 1, 2]]})
        history = [
            {"id": 11, "type": "episode", "watched_at": "1970-01-01T00:00:00.000Z",
             "episode": {"season": 1, "number": 1}, "show": {"ids": {"imdb": SID}}},
            {"id": 12, "type": "episode", "watched_at": "2026-02-01T20:00:00.000Z",
             "episode": {"season": 1, "number": 2}, "show": {"ids": {"imdb": SID}}},
        ]
        state = {"h": history}

        def on_add(body, params):
            state["h"] = state["h"] + [
                {"id": 13, "type": "episode", "watched_at": "2026-03-01T20:00:00.000Z",
                 "episode": {"season": 1, "number": 1}, "show": {"ids": {"imdb": SID}}}]
            return history_ok(body, params)

        def on_remove(body, params):
            state["h"] = [h for h in state["h"] if h["id"] not in body["ids"]]
            return remove_ok(body, params)

        fake = self.fake({
            ("GET", f"/sync/history/shows/{SID}"): lambda body, params: state["h"],
            ("POST", "/sync/history"): on_add,
            ("POST", "/sync/history/remove"): on_remove,
        })
        args = argparse.Namespace(yes=True, only=None, date=None)
        with contextlib.redirect_stdout(io.StringIO()):
            rc = b.cmd_redate(args)
        self.assertEqual(rc, common.EXIT_OK)
        posts = fake.posts()
        self.assertEqual([p for p, _ in posts], ["/sync/history", "/sync/history/remove"])
        added = posts[0][1]["shows"][0]["seasons"][0]["episodes"]
        self.assertEqual(added, [{"number": 1, "watched_at": "2026-03-01T20:00:00.000Z"}])
        self.assertEqual(posts[1][1], {"ids": [11]}, "dated scrobble 12 must stay")
        journal = json.loads((helpers.TMP_HOME / "last_redate.json").read_text())
        self.assertEqual(journal["shows"][0]["status"], "done")


if __name__ == "__main__":
    unittest.main()
