"""Shared peak interval features."""

from pathlib import Path
import sys
import unittest

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from beatdance.signal_processing import peak_intervals


class PeakIntervalTests(unittest.TestCase):
    def test_later_peak_time_and_variable_intervals(self):
        # Unsorted input, including a non-peak between two peaks.
        frame = pd.DataFrame(dict(time=[2.2, 0.5, 1.0, 0.8], is_peak=[1, 1, 1, 0]))
        result = peak_intervals(frame)
        np.testing.assert_allclose(result["time"], [1.0, 2.2])
        np.testing.assert_allclose(result["previous_peak_time"], [0.5, 1.0])
        np.testing.assert_allclose(result["delta_t"], [0.5, 1.2])

    def test_no_intervals_across_signal_boot_or_gap(self):
        frame = pd.DataFrame([
            ("X", 0, 0, 0.2, 1), ("X", 0, 0, 0.7, 1),
            ("X", 0, 1, 4.0, 1), ("X", 0, 1, 4.8, 1),
            ("X", 1, 0, 0.1, 1), ("X", 1, 0, 0.7, 1),
            ("Y", 0, 0, 0.3, 1), ("Y", 0, 0, 0.7, 1),
        ], columns=["signal", "segment", "span", "time", "is_peak"])
        result = peak_intervals(frame.sample(frac=1, random_state=42))
        self.assertEqual(len(result), 4)
        np.testing.assert_allclose(result["delta_t"], [0.5, 0.8, 0.6, 0.4])
        self.assertEqual(result["segment"].tolist(), [0, 0, 1, 0])
        self.assertEqual(result["span"].tolist(), [0, 1, 0, 0])

    def test_duplicate_times_are_ignored_and_input_is_unchanged(self):
        frame = pd.DataFrame(dict(time=[1, 0.5, 0.5], is_peak=[1, 1, 1]))
        original = frame.copy(deep=True)
        result = peak_intervals(frame)
        np.testing.assert_allclose(result["delta_t"], [0.5])
        pd.testing.assert_frame_equal(frame, original)

    def test_insufficient_peaks_and_empty_frame_keep_export_schema(self):
        for flags in ([], [0, 0], [1, 0], [1, 1]):
            # Each peak is in a separate span in the final case.
            frame = pd.DataFrame(dict(time=[0, 0.5][:len(flags)], is_peak=flags,
                                      span=list(range(len(flags)))))
            with self.subTest(flags=flags):
                result = peak_intervals(frame)
                self.assertTrue(result.empty)
                self.assertEqual(list(result.columns),
                                 ["span", "time", "previous_peak_time", "delta_t"])



if __name__ == "__main__":
    unittest.main()
