"""Activity segmentation: dropping rest fragments from detected events."""

from pathlib import Path
import sys
import unittest

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from beatdance.segments import (SEGMENT_COLUMNS, ActivityConfig, active_mask,
                                split_active_segments)


def events(period, count, start=0.0, jitter=0.0):
    """Evenly spaced event times, optionally with a fixed per-step offset."""
    return start + np.arange(count) * period + np.arange(count) * jitter


class ConfigTests(unittest.TestCase):
    def test_defaults_and_serialisation(self):
        config = ActivityConfig()
        self.assertEqual(config.gap_factor, 2.5)
        self.assertEqual(config.min_cycles, 3)
        self.assertEqual(config.to_dict(),
                         {"gap_factor": 2.5, "min_cycles": 3})

    def test_invalid_values_are_rejected(self):
        for kwargs in ({"gap_factor": 1.0}, {"gap_factor": 0.5},
                       {"gap_factor": float("nan")}, {"gap_factor": True},
                       {"min_cycles": 0}, {"min_cycles": 2.0},
                       {"min_cycles": True}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    ActivityConfig(**kwargs)


class SegmentTests(unittest.TestCase):
    def test_empty_input_keeps_the_export_schema(self):
        result = split_active_segments([])
        self.assertTrue(result.empty)
        self.assertEqual(list(result.columns), SEGMENT_COLUMNS)

    def test_two_runs_separated_by_a_rest_gap(self):
        # 5 cycles at 0.5 s, a 4 s rest, then 5 more cycles at 0.5 s.
        times = np.concatenate([events(0.5, 6), events(0.5, 6, start=6.5)])
        result = split_active_segments(times)

        self.assertEqual(len(result), 2)
        self.assertEqual(result["n_cycles"].tolist(), [5, 5])
        self.assertTrue(result["is_retained"].all())
        np.testing.assert_allclose(result["start_time"], [0.0, 6.5])

    def test_short_fragment_is_reported_but_not_retained(self):
        # 1 cycle, a rest, then 6 cycles.
        times = np.concatenate([events(0.5, 2), events(0.5, 7, start=6.0)])
        result = split_active_segments(times)

        self.assertEqual(len(result), 2)
        self.assertEqual(result["n_cycles"].tolist(), [1, 6])
        self.assertEqual(result["is_retained"].tolist(), [False, True])
        self.assertEqual(int(active_mask(times).sum()), 7)

    def test_threshold_is_strict(self):
        # A gap exactly equal to gap_factor * period must not split the run.
        times = np.array([0.0, 0.5, 1.0, 2.25, 2.75, 3.25])
        result = split_active_segments(times, config=ActivityConfig(gap_factor=2.5))
        self.assertEqual(len(result), 1)
        self.assertEqual(int(result["n_cycles"].iloc[0]), 5)

    def test_spans_never_merge(self):
        # Same times in two different spans: must stay two separate groups.
        times = np.array([0.0, 0.5, 1.0, 1.5, 0.0, 0.5, 1.0, 1.5])
        spans = np.array([0, 0, 0, 0, 1, 1, 1, 1])
        result = split_active_segments(times, spans)

        self.assertEqual(sorted(result["span"].tolist()), [0, 1])
        self.assertTrue(result["is_retained"].all())

    def test_unsorted_input_is_indexed_back_to_original_positions(self):
        times = np.array([1.0, 0.0, 1.5, 2.0, 0.5, 2.5])
        result = split_active_segments(times)

        self.assertEqual(len(result), 1)
        mask = active_mask(times)
        np.testing.assert_array_equal(mask, np.ones(6, dtype=bool))
        # first_index points at the earliest event in the original array.
        self.assertEqual(int(result["first_index"].iloc[0]), 1)
        self.assertEqual(times[int(result["last_index"].iloc[0])], 2.5)

    def test_explicit_reference_period_overrides_the_median(self):
        # Half-rate detection: intervals alternate 0.48 / 0.48 / 2.4 ...
        # The median (0.48) gives a 1.2 s threshold and cuts the 2.4 s rest.
        times = np.array([0.0, 0.48, 0.96, 3.36, 3.84, 4.32, 4.80])
        without = split_active_segments(times)
        self.assertEqual(len(without), 2)

        # With a deliberately large reference the gap is no longer "long".
        with_big = split_active_segments(times, reference_period=1.0)
        self.assertEqual(len(with_big), 1)

    def test_reference_period_validation(self):
        for bad in (0, -1, float("inf"), True):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    split_active_segments([0.0, 0.5], reference_period=bad)

    def test_invalid_times_are_rejected(self):
        for bad in ([0.0, np.nan], [0.0, np.inf]):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    split_active_segments(bad)

    def test_spans_must_match_times(self):
        with self.assertRaises(ValueError):
            split_active_segments([0.0, 0.5, 1.0], spans=[0, 0])

    def test_all_fragments_below_min_cycles_retains_nothing(self):
        times = np.array([0.0, 0.5, 5.0, 5.5, 9.0, 9.5])
        result = split_active_segments(times)
        self.assertTrue(result["is_retained"].eq(False).all())
        self.assertFalse(active_mask(times).any())

    def test_active_mask_matches_reported_segments(self):
        times = np.concatenate([
            events(0.48, 40),
            events(0.48, 3, start=25.0),
            events(0.48, 20, start=30.0),
        ])
        result = split_active_segments(times)
        mask = active_mask(times)

        expected = int(result.loc[result["is_retained"], "n_events"].sum())
        self.assertEqual(int(mask.sum()), expected)
        self.assertEqual(result["is_retained"].tolist(), [True, False, True])

    def test_real_shaped_input_counts_only_the_active_run(self):
        # 10 s of preparation peaks, then a clean 30 s run at 0.48 s.
        prep = np.array([1.2, 2.0, 4.5, 7.1, 8.9])
        run = events(0.48, 63, start=10.0)
        times = np.concatenate([prep, run])

        result = split_active_segments(times, config=ActivityConfig(min_cycles=3))
        kept = int(active_mask(times).sum())

        self.assertGreater(kept, 55)
        self.assertLess(kept, len(times))
        self.assertEqual(int(result.loc[result["is_retained"], "n_events"].sum()),
                         kept)


if __name__ == "__main__":
    unittest.main()
