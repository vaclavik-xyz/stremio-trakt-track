"""Unattended operation: token refresh, non-interactive guards, retries, watchdog,
report dedup, doctor. Network and clock are faked."""
import argparse
import contextlib
import io
import json
import os
import sys
import unittest
import urllib.error
from unittest import mock

import helpers
from fakes import FakeTrakt, write_json
import common
import cron_daily
import doctor
import stremio_bridge as b
import track

NOW = 1_800_000_000.0
DAY = 86400


def write_config(expires_in: float, refresh_token="r1"):
    write_json(helpers.TMP_HOME / "config.json", {
        "client_id": "cid", "client_secret": "sec",
        "token": {"access_token": "a1", "refresh_token": refresh_token,
                  "expires_at": NOW + expires_in}})


class Clocked(unittest.TestCase):
    def setUp(self):
        helpers.clean_home()
        for target, attr, val in ((common, "now", lambda: NOW), (common, "sleep", lambda s: None)):
            p = mock.patch.object(target, attr, val)
            p.start()
            self.addCleanup(p.stop)
        # never look at the real backup folder (iCloud) from tests
        p = mock.patch.dict(os.environ, {"TRAKT_BACKUP_DIR": str(helpers.TMP_HOME / "backups")})
        p.start()
        self.addCleanup(p.stop)


class TokenRefresh(Clocked):
    def fake(self, routes):
        fake = FakeTrakt(routes)
        p = mock.patch.object(track, "_req", fake)
        p.start()
        self.addCleanup(p.stop)
        return fake

    def test_refreshes_ahead_of_expiry(self):
        write_config(12 * 3600)
        fake = self.fake({("POST", "/oauth/token"): {"access_token": "a2", "refresh_token": "r2",
                                                     "expires_in": 7 * DAY}})
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(track.access_token(), "a2")
        self.assertEqual(fake.calls[0][2]["refresh_token"], "r1")
        cfg = json.loads((helpers.TMP_HOME / "config.json").read_text())
        self.assertEqual(cfg["token"]["expires_at"], int(NOW) + 7 * DAY)
        self.assertIn("refresh_ok_at", json.loads(track.AUTH_STATE.read_text()))

    def test_no_refresh_when_plenty_left(self):
        write_config(3 * DAY)
        fake = self.fake({})
        self.assertEqual(track.access_token(), "a1")
        self.assertEqual(fake.calls, [])

    def test_failed_refresh_with_valid_token_continues_and_records(self):
        write_config(12 * 3600)
        self.fake({("POST", "/oauth/token"): track.TraktError("HTTP 400: invalid_grant")})
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(track.access_token(), "a1")
        self.assertIn("python3 track.py auth", err.getvalue())
        state = json.loads(track.AUTH_STATE.read_text())
        self.assertIn("invalid_grant", state["refresh_error"])
        # cron turns it into a report line with the fix
        self.assertIn("track.py auth", cron_daily.auth_issue())

    def test_failed_refresh_with_expired_token_exits_auth(self):
        write_config(-60)
        self.fake({("POST", "/oauth/token"): track.TraktError("HTTP 401")})
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            track.access_token()
        self.assertEqual(cm.exception.code, common.EXIT_AUTH)
        self.assertIn("Náprava: python3 track.py auth", err.getvalue())

    def test_auth_check_rejected_token(self):
        write_config(3 * DAY)
        self.fake({("GET", "/users/settings"): track.TraktError("GET /users/settings -> HTTP 401: x")})
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            track.cmd_auth_check(argparse.Namespace())
        self.assertEqual(cm.exception.code, common.EXIT_AUTH)


class NonInteractive(Clocked):
    def test_device_flow_refuses_in_cron(self):
        write_config(3 * DAY)
        fake = FakeTrakt({})
        with mock.patch.dict(os.environ, {common.NONINTERACTIVE_ENV: "1"}), \
                mock.patch.object(track, "_req", fake), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            track.cmd_auth(argparse.Namespace())
        self.assertEqual(cm.exception.code, common.EXIT_AUTH)
        self.assertEqual(fake.calls, [])

    def test_stremio_login_refuses_in_cron(self):
        stdin = mock.Mock()
        stdin.read.side_effect = AssertionError("must not read a password")
        with mock.patch.dict(os.environ, {common.NONINTERACTIVE_ENV: "1"}), \
                mock.patch.object(sys, "stdin", stdin), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            b.cmd__login(argparse.Namespace(email="x@example.com"))
        self.assertEqual(cm.exception.code, common.EXIT_AUTH)

    def test_cron_children_cannot_wait_for_input(self):
        rc, out = cron_daily.run("-c", "import os; print(os.environ.get('%s')); input()"
                                 % common.NONINTERACTIVE_ENV)
        self.assertNotEqual(rc, 0)              # EOF immediately, no hang
        self.assertTrue(out.startswith("1"))

    def test_cron_steps_never_call_interactive_commands(self):
        import inspect
        src = inspect.getsource(cron_daily.daily)
        self.assertNotIn('"auth")', src)
        self.assertNotIn("_login", src)
        self.assertNotIn("setup_", src.replace("setup_stremio.sh", ""))

    def test_missing_stremio_login_is_loud(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            b.load_stremio()
        self.assertEqual(cm.exception.code, common.EXIT_AUTH)
        self.assertIn("setup_stremio.sh", err.getvalue())
        self.assertIn("nic nedoplňuje", err.getvalue())


class FakeResp:
    def __init__(self, body=b"[]"):
        self.body = body
        self.headers = {}

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(code):
    return urllib.error.HTTPError("https://api.trakt.tv/x", code, "err", {}, io.BytesIO(b"oops"))


class Retries(Clocked):
    def setUp(self):
        super().setUp()
        write_config(3 * DAY)
        self.sleeps = []
        p = mock.patch.object(common, "sleep", self.sleeps.append)
        p.start()
        self.addCleanup(p.stop)

    def test_get_retries_transient_errors(self):
        seq = [urllib.error.URLError("down"), http_error(503), FakeResp(b'[{"a": 1}]')]
        with mock.patch("urllib.request.urlopen", side_effect=seq) as uo:
            payload, _ = track._req("GET", "/x")
        self.assertEqual(payload, [{"a": 1}])
        self.assertEqual(uo.call_count, 3)
        self.assertEqual(self.sleeps, [2.0, 10.0])

    def test_get_gives_up_after_cap(self):
        with mock.patch("urllib.request.urlopen", side_effect=TimeoutError("t")) as uo:
            with self.assertRaises(track.TraktError):
                track._req("GET", "/x")
        self.assertEqual(uo.call_count, 4)

    def test_post_is_not_resent_after_ambiguous_error(self):
        with mock.patch("urllib.request.urlopen", side_effect=[TimeoutError("t"), FakeResp()]) as uo:
            with self.assertRaises(track.TraktError):
                track._req("POST", "/sync/history", body={"movies": []})
        self.assertEqual(uo.call_count, 1)

    def test_rate_limit_waits_for_post_too(self):
        e = urllib.error.HTTPError("u", 429, "lim", {"Retry-After": "5"}, io.BytesIO(b""))
        with mock.patch("urllib.request.urlopen", side_effect=[e, FakeResp(b"{}")]) as uo:
            track._req("POST", "/sync/history", body={})
        self.assertEqual(uo.call_count, 2)
        self.assertEqual(self.sleeps, [6.0])

    def test_client_error_not_retried(self):
        with mock.patch("urllib.request.urlopen", side_effect=http_error(404)) as uo:
            with self.assertRaises(track.TraktError):
                track._req("GET", "/x")
        self.assertEqual(uo.call_count, 1)

    def test_stremio_retries_then_fails_cleanly(self):
        import urllib.request as ur
        req = ur.Request("https://api.strem.io/api/x", data=b"{}", method="POST")
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")) as uo:
            with self.assertRaises(b.BridgeError):
                b._http_json(req, 5, "Stremio API")
        self.assertEqual(uo.call_count, 4)

    def test_stremio_session_error_is_auth(self):
        write_json(b.STREMIO_CFG, {"authKey": "k"})
        body = json.dumps({"error": {"message": "Session does not exist", "code": 1}}).encode()
        err = io.StringIO()
        with mock.patch("urllib.request.urlopen", return_value=FakeResp(body)), \
                contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            b.fetch_raw(force=True)
        self.assertEqual(cm.exception.code, common.EXIT_AUTH)
        self.assertIn("setup_stremio.sh", err.getvalue())


class Watchdog(Clocked):
    def test_stale_message(self):
        self.assertIsNone(common.stale_message())          # never ran yet
        write_json(common.LAST_SUCCESS, {"epoch": NOW - 10 * 3600})
        self.assertIsNone(common.stale_message())
        write_json(common.LAST_SUCCESS, {"epoch": NOW - 40 * 3600})
        self.assertIn("Tracking stojí", common.stale_message())


def fake_run(codes: dict):
    """cron_daily.run replacement: rc per step name, writes journals like the bridge."""
    def run(*argv, lock_fd=None):
        name = argv[1] if argv[0] == "stremio_bridge.py" else argv[1]
        rc = codes.get(name, 0)
        if name == "compare" and rc in (0, 10):
            write_json(cron_daily.GAPS, {"movies": [], "shows": [], "unknown": []})
        if name == "push" and rc in (0, 10):
            write_json(cron_daily.LAST_PUSH, {"movies": [], "shows": [], "denied": [], "repeat": []})
        if name == "forward" and rc in (0, 10):
            write_json(cron_daily.LAST_FORWARD, {"written": [], "denied": [], "repeat": [],
                                                 "skipped": [], "over_max": []})
        out = {5: "Stremio přihlášení neplatí (Session does not exist).\nNáprava: bash setup_stremio.sh"}
        return rc, out.get(rc, f"{name} rc {rc}")
    return run


class DailyRun(Clocked):
    def daily(self, codes, at=NOW):
        out = io.StringIO()
        with mock.patch.object(cron_daily, "run", fake_run(codes)), \
                mock.patch.object(common, "now", lambda: at), contextlib.redirect_stdout(out):
            cron_daily.daily(None)
        return out.getvalue()

    def test_success_is_silent_and_recorded(self):
        self.assertEqual(self.daily({}), "")
        data = json.loads(common.LAST_SUCCESS.read_text())
        self.assertEqual(data["epoch"], NOW)
        self.assertIn("sync", data["steps"])

    def test_failure_reports_once_then_after_three_days(self):
        out = self.daily({"fetch": 5})
        self.assertIn("setup_stremio.sh", out)
        self.assertIn("nic nedoplňuje", out)
        self.assertEqual(self.daily({"fetch": 5}, at=NOW + DAY), "")
        self.assertIn("setup_stremio.sh", self.daily({"fetch": 5}, at=NOW + 3 * DAY))

    def test_stale_tracking_is_reported(self):
        write_json(common.LAST_SUCCESS, {"epoch": NOW - 50 * 3600})
        out = self.daily({"compare": 1})
        self.assertIn("Tracking stojí", out)
        self.assertIn("compare selhalo", out)

    def test_success_clears_state_and_new_failure_is_new(self):
        self.daily({"push": 1})
        self.assertEqual(self.daily({}, at=NOW + DAY), "")
        self.assertIn("push selhalo", self.daily({"push": 1}, at=NOW + 2 * DAY))

    def test_unknown_survives_a_failed_compare_without_renotify(self):
        def with_unknown(codes):
            base = fake_run(codes)

            def run(*argv, lock_fd=None):
                rc, out = base(*argv, lock_fd=lock_fd)
                if argv[1] == "compare" and rc == 0:
                    write_json(cron_daily.GAPS, {"movies": [], "shows": [],
                                                 "unknown": [{"imdb": "tt1", "name": "S", "reason": "x"}]})
                return rc, out
            return run

        out = io.StringIO()
        with mock.patch.object(cron_daily, "run", with_unknown({})), contextlib.redirect_stdout(out):
            cron_daily.daily(None)
        self.assertIn("S: neověřeno", out.getvalue())
        self.daily({"compare": 1}, at=NOW + DAY)            # compare failed: unknown not dropped
        out = io.StringIO()
        with mock.patch.object(cron_daily, "run", with_unknown({})), \
                mock.patch.object(common, "now", lambda: NOW + 2 * DAY), contextlib.redirect_stdout(out):
            cron_daily.daily(None)
        self.assertNotIn("S: neověřeno", out.getvalue(), "already reported, unchanged")

    def test_issue_remind_zero_does_not_crash(self):
        cron_daily.issues_to_report({"a": "x"}, now=NOW, remind_days=0)
        cron_daily.issues_to_report({"a": "x"}, now=NOW + DAY, remind_days=0)


class Doctor(Clocked):
    def test_offline_fresh_install_warns(self):
        res = doctor.run_checks(offline=True)
        self.assertEqual(doctor.exit_code(res), common.EXIT_WARN)
        self.assertTrue(all(r["fix"] for r in res if r["status"] in (doctor.WARN, doctor.FAIL)))

    def test_offline_stale_fails(self):
        write_json(common.LAST_SUCCESS, {"epoch": NOW - 60 * 3600})
        res = doctor.run_checks(offline=True)
        self.assertEqual(doctor.exit_code(res), common.EXIT_ERROR)
        self.assertEqual(res[0]["status"], doctor.FAIL)

    def test_refresh_verified_by_call_when_no_proof(self):
        write_config(5 * DAY)
        fake = FakeTrakt({("POST", "/oauth/token"): {"access_token": "a2", "refresh_token": "r2",
                                                     "expires_in": 7 * DAY},
                          ("GET", "/users/settings"): {"user": {"username": "u"}}})
        with mock.patch.object(track, "_req", fake), contextlib.redirect_stderr(io.StringIO()):
            res = doctor.check_trakt(verify_refresh=False)
        self.assertEqual([c[1] for c in fake.calls], ["/oauth/token", "/users/settings"])
        self.assertTrue(all(r["status"] == doctor.OK for r in res), res)

    def test_refresh_not_repeated_with_recent_proof(self):
        write_config(5 * DAY)
        write_json(track.AUTH_STATE, {"refresh_ok_epoch": NOW - DAY})
        fake = FakeTrakt({("GET", "/users/settings"): {"user": {"username": "u"}}})
        with mock.patch.object(track, "_req", fake):
            doctor.check_trakt(verify_refresh=False)
        self.assertEqual([c[1] for c in fake.calls], ["/users/settings"])

    def test_broken_refresh_close_to_expiry_fails_with_fix(self):
        write_config(20 * 3600)
        fake = FakeTrakt({("POST", "/oauth/token"): track.TraktError("HTTP 400"),
                          ("GET", "/users/settings"): {"user": {"username": "u"}}})
        with mock.patch.object(track, "_req", fake), contextlib.redirect_stderr(io.StringIO()):
            res = doctor.check_trakt(verify_refresh=False)
        bad = [r for r in res if r["status"] == doctor.FAIL]
        self.assertTrue(bad)
        self.assertEqual(bad[0]["fix"], "python3 track.py auth")


if __name__ == "__main__":
    unittest.main()
