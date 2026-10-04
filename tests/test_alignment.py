"""Episode pairing between Cinemeta and Trakt numbering (made-up titles)."""
import unittest

import helpers  # noqa: F401  (isolates the data directory)
import stremio_bridge as b


def generic(n):
    return [(i, f"Episode {i}") for i in range(1, n + 1)]


def videos(season, titles):
    """Cinemeta-like video rows: [id, season, episode, title]."""
    return [[f"tt0000001:{season}:{e}", season, e, t] for e, t in titles]


class NormTitle(unittest.TestCase):
    def test_strips_part_marker_and_article(self):
        self.assertEqual(b._norm_title("The Heist (1)"), "heist")
        self.assertEqual(b._norm_title("Heist (Part 2)"), "heist")

    def test_keep_parts(self):
        self.assertEqual(b._norm_title("Heist (1)", keep_parts=True), "heist 1")

    def test_punctuation_and_case(self):
        self.assertEqual(b._norm_title("  Who's  There?!"), "who s there")

    def test_none(self):
        self.assertEqual(b._norm_title(None), "")


class AlignSeason(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(b.align_season([], generic(3)), [])
        self.assertEqual(b.align_season(generic(2), []), [None, None])

    def test_hour_long_double_merged_on_trakt(self):
        cm = [(1, "Pilot"), (2, "Heist (1)"), (3, "Heist (2)"), (4, "Aftermath")]
        tr = [(1, "Pilot"), (2, "Heist"), (3, "Aftermath")]
        self.assertEqual(b.align_season(cm, tr), [1, 2, 2, 3])

    def test_trakt_splits_like_cinemeta(self):
        cm = [(1, "Pilot"), (2, "Heist (1)"), (3, "Heist (2)"), (4, "Storm (1)"), (5, "Storm (2)")]
        tr = [(1, "Pilot"), (2, "Heist (1)"), (3, "Heist (2)"), (4, "Storm")]
        self.assertEqual(b.align_season(cm, tr), [1, 2, 3, 4, 4])

    def test_same_count_same_numbers_uses_numbers(self):
        cm = [(1, "Pilot"), (2, "Second"), (3, "Third")]
        tr = [(1, "Pilot"), (2, "Second one"), (3, "Third")]
        self.assertEqual(b.align_season(cm, tr), [1, 2, 3])

    def test_generic_titles_same_count(self):
        self.assertEqual(b.align_season(generic(12), generic(12)), list(range(1, 13)))

    def test_generic_titles_no_false_repeat(self):
        # Trakt does not know E12 yet: it must stay unmatched, not land on E11
        self.assertEqual(b.align_season(generic(12), generic(11)), list(range(1, 12)) + [None])

    def test_missing_titles_fall_back_to_order(self):
        cm = [(1, ""), (2, ""), (3, "")]
        tr = [(1, "A"), (2, "B"), (3, "C")]
        self.assertEqual(b.align_season(cm, tr), [1, 2, 3])


class MapPairs(unittest.TestCase):
    def setUp(self):
        names = ["Pilot", "Arrival", "Heist (1)", "Heist (2)", "Fallout", "Harbor", "Echoes",
                 "Lantern", "Quarry", "Mirage", "Tundra", "Vigil", "Zenith"]
        self.cm = list(enumerate(names, 1))                         # 13 Cinemeta entries
        merged = ["Pilot", "Arrival", "Heist", "Fallout", "Harbor", "Echoes",
                  "Lantern", "Quarry", "Mirage", "Tundra", "Vigil", "Zenith"]
        self.titles = {(1, e): t for e, t in enumerate(merged, 1)}  # 12 on Trakt

    def test_skipped_more_than_four(self):
        watched = {(1, 1), (1, 2), (1, 12)}        # Cinemeta E12 = "Vigil" = Trakt E11
        res = b.map_pairs(videos(1, self.cm), self.titles, watched)
        self.assertEqual(res["mapped"], {(1, 1), (1, 2), (1, 11)})
        self.assertEqual(res["unmatched"], [])

    def test_halves_counted_once(self):
        res = b.map_pairs(videos(1, self.cm), self.titles, {(1, 3), (1, 4)})
        self.assertEqual(res["mapped"], {(1, 3)})
        self.assertEqual(res["parts"], {(1, 3): 2})

    def test_generic_titles_episode_10_is_not_episode_3(self):
        cm = generic(4) + [(5, "Episode 5 (1)"), (6, "Episode 5 (2)")] + \
            [(i + 1, f"Episode {i}") for i in range(6, 13)]
        titles = {(1, e): t for e, t in generic(12)}
        watched = {(1, 1), (1, 2), (1, 3), (1, 11)}  # Cinemeta E11 = "Episode 10"
        res = b.map_pairs(videos(1, cm), titles, watched)
        self.assertEqual(res["mapped"], {(1, 1), (1, 2), (1, 3), (1, 10)})
        self.assertEqual(res["unmatched"], [])

    def test_season_unknown_to_trakt_is_unmatched(self):
        res = b.map_pairs(videos(2, [(1, "X")]), self.titles, {(2, 1)})
        self.assertEqual(res["mapped"], set())
        self.assertEqual(res["unmatched"], [(2, 1, "X")])

    def test_empty_input(self):
        res = b.map_pairs([], {}, set())
        self.assertEqual(res, {"mapped": set(), "unmatched": [], "parts": {}})


if __name__ == "__main__":
    unittest.main()
