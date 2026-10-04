"""restore (undo removals) and export. Trakt faked, temp files only."""
import argparse
import contextlib
import csv
import io
import json
import unittest
from unittest import mock

import helpers
from fakes import FakeTrakt, history_ok, write_json
import stremio_bridge as b
import track
import writes

SID = "tt0000001"
SHOW = {"imdb": SID, "trakt": 1}


def args(**kw):
    base = dict(from_journal=None, from_log=False, from_db=None, since=None, only=None,
                yes=True, force_repeat=False)
    base.update(kw)
    return argparse.Namespace(**base)


class Restore(unittest.TestCase):
    def setUp(self):
        helpers.clean_home()
        p = mock.patch.object(writes, "WRITE_SLEEP", 0)
        p.start()
        self.addCleanup(p.stop)

    def fake(self, history_eps):
        state = {"eps": list(history_eps)}

        def on_add(body, params):
            for sh in body["shows"]:
                for s in sh["seasons"]:
                    for e in s["episodes"]:
                        state["eps"].append({"watched_at": e["watched_at"], "show": {"ids": SHOW},
                                             "episode": {"season": s["number"], "number": e["number"]}})
            return history_ok(body, params)

        fake = FakeTrakt({("GET", "/sync/history/movies"): [],
                          ("GET", "/sync/history/episodes"): lambda b_, p_: state["eps"],
                          ("POST", "/sync/history"): on_add})
        p = mock.patch.object(track, "_req", fake)
        p.start()
        self.addCleanup(p.stop)
        return fake

    def removed_entry(self, hid, ep, when):
        return {"history_id": hid, "kind": "episode", "ids": SHOW, "season": 1, "episode": ep,
                "watched_at": when, "name": "Show"}

    def test_from_journal_restores_original_dates_once(self):
        write_json(helpers.TMP_HOME / "last_exact.json", {
            "removed": [self.removed_entry(7, 2, "2026-01-02T20:00:00.000Z"),
                        self.removed_entry(8, 3, "1970-01-01T00:00:00.000Z")]})
        fake = self.fake([])
        with contextlib.redirect_stdout(io.StringIO()):
            b.cmd_restore(args(from_journal=str(helpers.TMP_HOME / "last_exact.json")))
        body = fake.posts()[0][1]
        eps = {e["number"]: e["watched_at"] for e in body["shows"][0]["seasons"][0]["episodes"]}
        self.assertEqual(eps, {2: "2026-01-02T20:00:00.000Z", 3: "unknown"})
        with contextlib.redirect_stdout(io.StringIO()):
            b.cmd_restore(args(from_journal=str(helpers.TMP_HOME / "last_exact.json")))
        self.assertEqual(len(fake.posts()), 1, "already restored entries are skipped")

    def test_from_log_after_exact(self):
        writes.append_log([writes._record("remove", "exact",
                                          self.removed_entry(9, 4, "2026-02-01T21:00:00.000Z"))])
        fake = self.fake([])
        with contextlib.redirect_stdout(io.StringIO()):
            b.cmd_restore(args(from_log=True))
        eps = fake.posts()[0][1]["shows"][0]["seasons"][0]["episodes"]
        self.assertEqual(eps, [{"number": 4, "watched_at": "2026-02-01T21:00:00.000Z"}])

    def test_from_db_snapshot_and_dry_run(self):
        snap = helpers.TMP_HOME / "snap.sqlite"
        con = track.db(snap)
        con.execute(f"INSERT INTO watched_episodes({track.EPISODE_COLS}) "
                    "VALUES(1,1,'Show',1,1,'E1','2026-01-01T20:00:00.000Z','watch',30)")
        con.execute(f"INSERT INTO watched_episodes({track.EPISODE_COLS}) "
                    "VALUES(2,1,'Show',1,2,'E2','2026-01-02T20:00:00.000Z','watch',30)")
        con.commit()
        con.close()
        present = [{"watched_at": "2026-01-01T20:00:00.000Z", "show": {"ids": SHOW},
                    "episode": {"season": 1, "number": 1}}]
        fake = self.fake(present)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            b.cmd_restore(args(from_db=str(snap), yes=False))
        self.assertIn("v Traktu chybí 1", out.getvalue())
        self.assertEqual(fake.posts(), [])
        with contextlib.redirect_stdout(io.StringIO()):
            b.cmd_restore(args(from_db=str(snap)))
        body = fake.posts()[0][1]
        self.assertEqual(body["shows"][0]["ids"], {"trakt": 1})
        self.assertEqual(body["shows"][0]["seasons"][0]["episodes"],
                         [{"number": 2, "watched_at": "2026-01-02T20:00:00.000Z"}])


class Export(unittest.TestCase):
    def setUp(self):
        helpers.clean_home()
        con = track.db()
        con.execute(f"INSERT INTO watched_movies({track.MOVIE_COLS}) "
                    "VALUES(1,1,'Kept',2000,'2026-01-01T10:00:00.000Z','watch','tt0000001',NULL,90)")
        con.execute(f"INSERT INTO watched_movies({track.MOVIE_COLS}, deleted_at) "
                    "VALUES(2,2,'Gone',2001,'2026-01-02T10:00:00.000Z','watch','tt0000002',NULL,95,"
                    "'2026-02-01T00:00:00+00:00')")
        con.commit()
        con.close()

    def test_csv_live_only(self):
        out = helpers.TMP_HOME / "export"
        with contextlib.redirect_stdout(io.StringIO()):
            track.cmd_export(argparse.Namespace(format="csv", out=str(out), include_deleted=False))
        rows = list(csv.DictReader((out / "movies.csv").open()))
        self.assertEqual([r["title"] for r in rows], ["Kept"])
        self.assertNotIn("deleted_at", rows[0])
        self.assertTrue((out / "episodes.csv").exists())

    def test_json_with_deleted(self):
        target = helpers.TMP_HOME / "x.json"
        with contextlib.redirect_stdout(io.StringIO()):
            track.cmd_export(argparse.Namespace(format="json", out=str(target), include_deleted=True))
        data = json.loads(target.read_text())
        self.assertEqual([m["title"] for m in data["movies"]], ["Kept", "Gone"])
        self.assertEqual(data["movies"][1]["deleted_at"], "2026-02-01T00:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
