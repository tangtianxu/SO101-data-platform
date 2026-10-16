"""Count validation only: no recording, camera or serial connections."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from capture_counts import MAX_EPISODES, episode_count
from common import validate_config


class CaptureCountTests(unittest.TestCase):
    def test_large_counts_are_positive_integers_not_limited_by_experiment_size(self):
        for value in (1, 31, 50, 200, 1000, 10000, MAX_EPISODES):
            with self.subTest(value=value):
                self.assertEqual(episode_count(value), value)
                self.assertEqual(episode_count(str(value)), value)

    def test_integral_numeric_form_values_are_accepted(self):
        for value in (50.0, " 200 ", "50.0", "2e2"):
            with self.subTest(value=value):
                self.assertEqual(episode_count(value), int(float(value)))

    def test_invalid_or_imprecise_counts_are_rejected_without_rounding(self):
        for value in (0, -1, True, False, None, "", " ", [], {}, 1.2, "1.2",
                      float("nan"), float("inf"), "NaN", "Infinity",
                      MAX_EPISODES + 1, str(MAX_EPISODES + 1), "1e99999",
                      "9007199254740990.1", "9007199254740991.1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                episode_count(value)

    def test_saved_config_accepts_large_counts(self):
        for count in (50, 200, 10000):
            with self.subTest(count=count):
                self.assertEqual(validate_config({"episodes": count})["episodes"], count)

    def test_config_rejects_invalid_counts(self):
        for count in (0, -1, 1.2, None, True, MAX_EPISODES + 1):
            with self.subTest(count=count), self.assertRaises(ValueError):
                validate_config({"episodes": count})


if __name__ == "__main__":
    unittest.main()
