"""Interval features and the shared dashboard's Δt–t display."""

from pathlib import Path
import sys
import unittest
from unittest.mock import patch

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

    def test_dashboard_plots_interval_coordinates_and_handles_no_intervals(self):
        from streamlit.testing.v1 import AppTest
        frame = pd.DataFrame(dict(time=[0.2, 0.7, 1.5, 3, 3.6],
                                  raw_acceleration=[10] * 5,
                                  filtered_acceleration=[10] * 5,
                                  is_peak=[1] * 5, span=[0, 0, 0, 1, 1]))
        with patch("pandas.read_csv", return_value=frame), patch("streamlit.pyplot") as plot:
            app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=25).run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(plot.call_count, 2)
        axis = plot.call_args_list[1].args[0].axes[0]
        np.testing.assert_allclose(axis.lines[0].get_xdata(), [0.7, 1.5])
        np.testing.assert_allclose(axis.lines[0].get_ydata(), [0.5, 0.8])
        np.testing.assert_allclose(axis.lines[1].get_xdata(), [3.6])
        np.testing.assert_allclose(axis.lines[1].get_ydata(), [0.6])
        np.testing.assert_allclose(axis.lines[2].get_ydata(), [1.9 / 3] * 2)
        self.assertEqual(app.metric[2].value, "0.633")
        table = next(table.value for table in app.dataframe if "delta_t" in table.value)
        np.testing.assert_allclose(table["delta_t"], [0.5, 0.8, 0.6])
        frame["is_peak"] = [1, 0, 0, 1, 0]
        with patch("pandas.read_csv", return_value=frame), patch("streamlit.pyplot") as plot:
            app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=25).run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(plot.call_count, 1)
        self.assertTrue(any("尚无有效间隔" in item.value for item in app.info))


if __name__ == "__main__":
    unittest.main()
