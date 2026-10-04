"""Regression tests for the last batch of Low findings."""
import argparse
import contextlib
import io
import json
import os
import stat
import subprocess
import unittest
from unittest import mock

import helpers
from fakes import FakeTrakt, bitfield, write_json
import backup_db
import common
import stremio_bridge as b
import track
import writes


class SetupSecret(unittest.TestCase):
    def setUp(self):
        helpers.clean_home()

    def run_setup(self, answers: str):
        env = {**os.environ, "TRAKT_TRACKER_HOME": str(helpers.TMP_HOME)}
        return subprocess.run(["bash", str(helpers.CODE_DIR / "setup_secret.sh")], input=answers,
                              env=env, capture_output=True, text=True, timeout=30)

    def test_enter_keeps_stored_secret(self):
        self.assertEqual(self.run_setup("cid\ns1\n\n").returncode, 0)
        # znovu, jen kvůli redirect URI: přepsat = y, stejné ID, Enter u secretu
        p = self.run_setup("y\ncid\n\nhttps://example.com/cb\n")
        self.assertEqual(p.returncode, 0, p.stderr)
        cfg = json.loads((helpers.TMP_HOME / "config.json").read_text())
        self.assertEqual(cfg["client_secret"], "s1")
        self.assertEqual(cfg["redirect_uri"], "https://example.com/cb")
        self.assertEqual(stat.S_IMODE(os.stat(helpers.TMP_HOME / "config.json").st_mode), 0o600)

    def test_new_secret_replaces_old(self):
        self.run_setup("cid\ns1\n\n")
        self.run_setup("y\ncid\ns2\n\n")
        cfg = json.loads((helpers.TMP_HOME / "config.json").read_text())
        self.assertEqual(cfg["client_secret"], "s2")


class RestoreSince(unittest.TestCase):
    def setUp(self):
        helpers.clean_home()

    def entry(self, hid, when):
        return {"history_id": hid, "kind": "episode", "ids": {"imdb": "tt0000001"}, "season": 1,
                "episode": hid, "watched_at": when, "name": "Show"}

    def test_journal_honours_since(self):
        path = helpers.TMP_HOME / "last_exact.json"
        write_json(path, {"removed": [self.entry(1, "2025-06-01T20:00:00.000Z"),
                                      self.entry(2, "2026-02-01T20:00:00.000Z")]})
        got = b.restore_candidates_from_journal(path, "2026-01-01")
        self.assertEqual([e["history_id"] for e in got], [2])
        self.assertEqual(len(b.restore_candidates_from_journal(path)), 2)

    def test_since_means_watched_at_for_every_source(self):
        writes.append_log([writes._record("remove", "exact", self.entry(1, "2025-06-01T20:00:00.000Z")),
                           writes._record("remove", "exact", self.entry(2, "2026-02-01T20:00:00.000Z"))])
        got = b.restore_candidates_from_log("2026-01-01")
        self.assertEqual([e["history_id"] for e in got], [2])

    def test_cli_journal_with_since_posts_only_matching(self):
        path = helpers.TMP_HOME / "last_exact.json"
        write_json(path, {"removed": [self.entry(1, "2025-06-01T20:00:00.000Z"),
                                      self.entry(2, "2026-02-01T20:00:00.000Z")]})
        fake = FakeTrakt({("GET", "/sync/history/movies"): [], ("GET", "/sync/history/episodes"): [],
                          ("POST", "/sync/history"): lambda body, params: {
                              "added": {"episodes": 1}, "not_found": {}}})
        args = argparse.Namespace(from_journal=str(path), from_log=False, from_db=None,
                                  since="2026-01-01", only=None, yes=True, force_repeat=False)
        with mock.patch.object(track, "_req", fake), mock.patch.object(writes, "WRITE_SLEEP", 0), \
                contextlib.redirect_stdout(io.StringIO()):
            b.cmd_restore(args)
        eps = fake.posts()[0][1]["shows"][0]["seasons"][0]["episodes"]
        self.assertEqual([e["number"] for e in eps], [2])


class SnapshotPermissions(unittest.TestCase):
    def setUp(self):
        helpers.clean_home()
        con = track.db()
        con.commit()
        con.close()

    def test_snapshot_is_created_private_not_chmodded_later(self):
        old = os.umask(0o022)
        try:
            # bez dodatečného chmod: o právech rozhoduje jen to, jak soubor vznikl
            with mock.patch.object(backup_db.os, "chmod", lambda *a, **k: None):
                out = backup_db.make_snapshot(helpers.TMP_HOME / "backups")
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(os.stat(out).st_mode), 0o600)
        self.assertEqual(os.umask(old), old, "umask restored")
        os.umask(old)


class ExactCinemetaDown(unittest.TestCase):
    def setUp(self):
        helpers.clean_home()

    def test_clean_message_and_no_trakt_calls(self):
        sid = "tt0000001"
        write_json(b.RAW, [{"_id": sid, "type": "series", "name": "Show",
                            "state": {"watched": bitfield(3, [0, 2], f"{sid}:1:3")}}])
        fake = FakeTrakt({})
        err = io.StringIO()
        with mock.patch.object(b, "cinemeta_videos",
                               side_effect=b.BridgeError("Cinemeta tt0000001: HTTP 503")), \
                mock.patch.object(track, "_req", fake), contextlib.redirect_stderr(err), \
                self.assertRaises(SystemExit) as cm:
            b.cmd_exact(argparse.Namespace(only=sid, yes=True, date=None, include_specials=False,
                                           allow_unmatched=False, force_repeat=False))
        self.assertEqual(cm.exception.code, common.EXIT_ERROR)
        self.assertIn("nic neměním", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())
        self.assertEqual(fake.calls, [])


class ReadmeToken(unittest.TestCase):
    def test_readme_states_real_token_lifetime(self):
        text = (helpers.CODE_DIR / "README.md").read_text()
        self.assertIn("valid for about a week", text)
        self.assertNotIn("three months", text)


if __name__ == "__main__":
    unittest.main()
