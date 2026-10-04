"""Local snapshot always, off-site copy best-effort with a hard time limit."""
import contextlib
import io
import os
import stat
import sys
import time
import unittest
from unittest import mock

import helpers
from fakes import write_json
import backup_db
import common
import doctor
import track


class Base(unittest.TestCase):
    def setUp(self):
        helpers.clean_home()
        con = track.db()
        con.execute(f"INSERT INTO watched_movies({track.MOVIE_COLS}) "
                    "VALUES(1,1,'M',2000,'2026-01-01T10:00:00.000Z','watch','tt0000001',NULL,90)")
        con.commit()
        con.close()
        self.local = helpers.TMP_HOME / "backups"
        self.offsite = helpers.TMP_HOME / "offsite"
        p = mock.patch.dict(os.environ, {"TRAKT_BACKUP_DIR": str(self.offsite)})
        p.start()
        self.addCleanup(p.stop)


class Offsite(Base):
    def test_success_copies_verified_snapshot(self):
        meta = backup_db.backup(self.local, self.offsite)
        self.assertTrue(meta["offsite"], meta)
        copied = backup_db.snapshots(self.offsite)
        self.assertEqual(len(copied), 1)
        self.assertEqual(stat.S_IMODE(os.stat(copied[0]).st_mode), 0o600)
        self.assertEqual(list(self.offsite.glob(".*.partial")), [])

    def test_hanging_target_finishes_within_limit(self):
        sleeper = [sys.executable, "-c", "import time; time.sleep(60)"]
        with mock.patch.object(backup_db, "_self", lambda *a: sleeper), \
                mock.patch.object(backup_db, "offsite_timeout", lambda: 1.0):
            t0 = time.monotonic()
            meta = backup_db.backup(self.local, self.offsite)
            took = time.monotonic() - t0
        self.assertLess(took, 6)
        self.assertFalse(meta["offsite"])
        self.assertIn("neodpověděl do 1 s", meta["offsite_reason"])
        self.assertEqual(len(backup_db.snapshots(self.local)), 1, "local snapshot always exists")
        saved = common.read_json(self.local / "last_backup.json", {})
        self.assertIs(saved["offsite"], False)

    def test_interrupted_system_call_is_a_clean_warning(self):
        out = io.StringIO()
        with mock.patch.object(backup_db, "offsite_copy",
                               side_effect=InterruptedError(4, "Interrupted system call")), \
                contextlib.redirect_stdout(out):
            rc = backup_db.worker_main(["--_offsite-copy", "a", "b"])
        self.assertEqual(rc, 1)
        self.assertEqual(out.getvalue().strip(),
                         "cíl nedostupný: InterruptedError: [Errno 4] Interrupted system call")
        with mock.patch.object(backup_db, "offsite_probe", side_effect=InterruptedError(4, "x")), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(backup_db.worker_main(["--_offsite-probe", "b"]), 1)

    def test_unusable_target_end_to_end(self):
        self.offsite.write_text("not a directory")          # mkdir fails in the child
        meta = backup_db.backup(self.local, self.offsite)
        self.assertFalse(meta["offsite"])
        self.assertTrue(meta["offsite_reason"].startswith("cíl nedostupný:"), meta["offsite_reason"])
        self.assertNotIn("Traceback", meta["offsite_reason"])

    def test_disabled_offsite(self):
        meta = backup_db.backup(self.local, None)
        self.assertFalse(meta["offsite"])
        self.assertIn("vypnuto", meta["offsite_reason"])

    def test_main_rc_zero_and_warning_not_repeated(self):
        self.offsite.write_text("x")

        def run_main():
            out = io.StringIO()
            with mock.patch.object(sys, "argv", ["backup_db.py", "--quiet"]), \
                    contextlib.redirect_stdout(out):
                backup_db.main()          # returns normally → rc 0 although off-site failed
            return out.getvalue()

        out = run_main()
        self.assertIn("Mimo stroj se nezálohuje", out)
        out = run_main()
        self.assertNotIn("Mimo stroj se nezálohuje", out, "same reason next night stays quiet")


class DoctorBackup(Base):
    def rows(self):
        return {r["name"]: r for r in doctor.check_backup()}

    def test_missing_local_snapshot_fails(self):
        r = self.rows()["Záloha lokálně"]
        self.assertEqual(r["status"], doctor.FAIL)
        self.assertIn("backup_db.py", r["fix"])

    def test_old_local_snapshot_fails(self):
        backup_db.make_snapshot(self.local)
        snap = backup_db.snapshots(self.local)[0]
        old = time.time() - 3 * 86400
        os.utime(snap, (old, old))
        self.assertEqual(self.rows()["Záloha lokálně"]["status"], doctor.FAIL)

    def test_unreachable_offsite_is_explained_warning(self):
        backup_db.make_snapshot(self.local)
        with mock.patch.object(backup_db, "probe_offsite",
                               return_value=(None, "cíl neodpověděl do 10 s (nedostupný?)")):
            rows = self.rows()
        self.assertEqual(rows["Záloha lokálně"]["status"], doctor.OK)
        r = rows["Záloha mimo stroj"]
        self.assertEqual(r["status"], doctor.WARN)
        self.assertIn("mimo stroj se nezálohuje (cíl nedostupný", r["detail"])
        self.assertIn("backup_dir", r["fix"])

    def test_probe_of_hanging_target_is_bounded(self):
        sleeper = [sys.executable, "-c", "import time; time.sleep(60)"]
        with mock.patch.object(backup_db, "_self", lambda *a: sleeper):
            t0 = time.monotonic()
            info, err = backup_db.probe_offsite(self.offsite, timeout=1)
        self.assertLess(time.monotonic() - t0, 6)
        self.assertIsNone(info)
        self.assertIn("neodpověděl", err)

    def test_healthy_offsite_ok(self):
        backup_db.backup(self.local, self.offsite)
        rows = self.rows()
        self.assertEqual(rows["Záloha mimo stroj"]["status"], doctor.OK, rows)


if __name__ == "__main__":
    unittest.main()
