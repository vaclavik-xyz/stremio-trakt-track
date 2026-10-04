"""Stremio watched bitfield: decoding and re-anchoring (made-up IDs)."""
import base64
import unittest
import zlib
from unittest import mock

import helpers  # noqa: F401
from fakes import bitfield
import stremio_bridge as b

SID = "tt0000001"


def vids(pairs):
    return [[f"{SID}:{s}:{e}", s, e, f"T{s}{e}"] for s, e in pairs]


class Decode(unittest.TestCase):
    def test_lsb_first_and_anchor(self):
        wb = b.decode_watched(bitfield(10, [0, 2, 9], f"{SID}:1:10"))
        self.assertEqual(wb["bits"], [0, 2, 9])
        self.assertEqual(wb["n"], 10)
        self.assertEqual((wb["sid"], wb["season"], wb["episode"]), (SID, 1, 10))
        self.assertEqual(wb["anchor"], f"{SID}:1:10")

    def test_single_byte_bit_order(self):
        payload = base64.b64encode(zlib.compress(bytes([0b00000101]))).decode()
        self.assertEqual(b.decode_watched(f"{SID}:1:3:3:{payload}")["bits"], [0, 2])

    def test_non_standard_anchor(self):
        wb = b.decode_watched(bitfield(3, [2], "yt_id:UCabc:xyz"))
        self.assertEqual(wb["anchor"], "yt_id:UCabc:xyz")
        self.assertIsNone(wb["season"])

    def test_corrupt_payload_raises(self):
        with self.assertRaises(ValueError):
            b.decode_watched(f"{SID}:1:3:3:@@@not-base64@@@")
        with self.assertRaises(ValueError):
            b.decode_watched(f"{SID}:1:3:3:" + base64.b64encode(b"not zlib").decode())
        with self.assertRaises(ValueError):
            b.decode_watched("garbage")


class Realign(unittest.TestCase):
    def test_no_change(self):
        ids = [v[0] for v in vids([(1, 1), (1, 2), (1, 3)])]
        self.assertEqual(b.realign([0, 2], 3, f"{SID}:1:3", ids), [0, 2])

    def test_special_inserted_before(self):
        # mapa vznikla nad [S1E1, S1E2, S1E3]; Cinemeta teď dává speciál S0E1 na začátek
        ids = [v[0] for v in vids([(0, 1), (1, 1), (1, 2), (1, 3)])]
        self.assertEqual(b.realign([0, 2], 3, f"{SID}:1:3", ids), [1, 3])

    def test_new_episodes_appended(self):
        ids = [v[0] for v in vids([(1, 1), (1, 2), (1, 3), (1, 4), (1, 5)])]
        self.assertEqual(b.realign([0, 1, 2], 3, f"{SID}:1:3", ids), [0, 1, 2])

    def test_video_removed_before_anchor(self):
        ids = [v[0] for v in vids([(1, 2), (1, 3)])]       # S1E1 zmizel
        self.assertEqual(b.realign([0, 1, 2], 3, f"{SID}:1:3", ids), [0, 1])

    def test_anchor_missing(self):
        ids = [v[0] for v in vids([(1, 1)])]
        self.assertIsNone(b.realign([0], 1, f"{SID}:9:9", ids))


class State(unittest.TestCase):
    def series(self, n, bits, anchor_ep):
        return {"_id": SID, "type": "series", "name": "Show",
                "state": {"watched": bitfield(n, bits, f"{SID}:1:{anchor_ep}")}}

    def test_realigned_series_maps_correctly(self):
        videos = vids([(0, 1), (1, 1), (1, 2), (1, 3)])
        with mock.patch.object(b, "cinemeta_videos", lambda sid, refresh=False: videos):
            st = b.stremio_state(self.series(3, [0, 2], 3))
        self.assertTrue(st["verified"])
        self.assertEqual(st["watched"], {(1, 1), (1, 3)})
        self.assertEqual(st["realigned"], -1)

    def test_stale_cache_is_refreshed_once(self):
        calls = []

        def cm(sid, refresh=False):
            calls.append(refresh)
            return vids([(1, 1), (1, 2)]) if not refresh else vids([(1, 1), (1, 2), (1, 3)])

        with mock.patch.object(b, "cinemeta_videos", cm):
            st = b.stremio_state(self.series(3, [2], 3))
        self.assertEqual(calls, [False, True])
        self.assertEqual(st["watched"], {(1, 3)})

    def test_anchor_unknown_is_unverified(self):
        with mock.patch.object(b, "cinemeta_videos", lambda sid, refresh=False: vids([(1, 1)])):
            st = b.stremio_state(self.series(3, [2], 3))
        self.assertFalse(st["verified"])
        self.assertIn("error", st)

    def test_movie_only_counted_when_really_watched(self):
        base = {"_id": "tt0000002", "type": "movie", "name": "M"}
        opened = {**base, "state": {"lastWatched": "2026-01-01T20:00:00.000Z", "timesWatched": 0}}
        self.assertEqual(b.stremio_state(opened)["watched"], set())
        played = {**base, "state": {"timesWatched": 1}}
        self.assertEqual(b.stremio_state(played)["watched"], {(0, 0)})
        flagged = {**base, "state": {"flaggedWatched": 1}}
        self.assertEqual(b.stremio_state(flagged)["watched"], {(0, 0)})

    def test_non_movie_types_ignored(self):
        for item in ({"_id": "tt0000002", "type": "channel", "state": {"timesWatched": 3}},
                     {"_id": "kitsu:123", "type": "movie", "state": {"timesWatched": 3}}):
            self.assertFalse(b.is_movie(item))
            self.assertEqual(b.stremio_state(item)["watched"], set())


if __name__ == "__main__":
    unittest.main()
