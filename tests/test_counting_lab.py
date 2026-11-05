"""Synthetic contract checks, not evidence of real jump-rope accuracy."""
import json
import tempfile
import unittest
from pathlib import Path
import numpy as np
import pandas as pd
from streamlit.testing.v1 import AppTest
from unittest.mock import patch
from beatdance.signal_processing import AlgorithmConfig, analyze_imu
from beatdance.counting import detect, PRESETS, BASELINE
from infra.store import Store, read_json
from infra.demo import install_demo
from infra.experiments import ExperimentConfig, ExperimentJob
from infra.counting_lab import BatchJob, load_comparison, paired_cases, meeting_report

class DetectorTests(unittest.TestCase):
    def frame(self, period=0.5, amplitude=1.0):
        t = np.arange(0, 8, 0.02)
        return pd.DataFrame(dict(time=t, segment=0, ax_g=0., ay_g=0., az_g=1+amplitude*np.sin(2*np.pi*t/period)))

    def test_baseline_identical_and_configs_roundtrip(self):
        data = self.frame()
        pd.testing.assert_frame_equal(detect(data).analysis, analyze_imu(data))
        for a in PRESETS:
            c = ExperimentConfig(algorithm_id=a)
            self.assertEqual(ExperimentConfig.from_dict(c.to_dict()), c)
        for p in ({"interval_ratio": 1}, {"window_seconds": 0}, {"high_sigma": -1}, {"bogus": 1}):
            with self.assertRaises(ValueError):
                detect(data, params=p)

    def test_periodic_static_and_gap_reset(self):
        for a in PRESETS:
            result = detect(self.frame(amplitude=.2), algorithm_id=a)
            count = int(result.analysis.is_peak.sum())
            self.assertTrue(13 <= count <= 17, (a, count))
            self.assertEqual(int(detect(self.frame(amplitude=0), algorithm_id=a).analysis.is_peak.sum()), 0)
            data = self.frame(amplitude=.2)
            data = data[(data.time < 3) | (data.time > 4)]
            result = detect(data, algorithm_id=a)
            self.assertEqual(result.analysis.span.nunique(), 2)
            self.assertFalse(result.analysis.time.between(3, 4).any())

    def test_adaptive_interval_fast_and_amplitude_small(self):
        fast = self.frame(period=.30, amplitude=.2)
        self.assertGreater(detect(fast, algorithm_id="adaptive-interval-v1").analysis.is_peak.sum(), detect(fast).analysis.is_peak.sum())
        small = self.frame(amplitude=.04)
        self.assertGreater(detect(small, algorithm_id="adaptive-amplitude-v1").analysis.is_peak.sum(), detect(small).analysis.is_peak.sum())

class LabTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        _, _, self.dataset = install_demo(self.store)

    def test_batch_compare_report_and_source_compatibility(self):
        batch = BatchJob(self.store, self.dataset["id"], [ExperimentConfig(algorithm_id=a) for a in PRESETS], "test <script>").start()
        batch.thread.join(60)
        self.assertFalse(batch.thread.is_alive())
        state = batch.snapshot()
        self.assertEqual(state["status"], "completed", state)
        runs = load_comparison(self.store, state["runs"])
        self.assertEqual(len(runs), 5)
        paired = paired_cases(runs[0]["cases"], runs[1]["cases"])
        self.assertFalse(paired.empty)
        self.assertIn("data:image/png;base64", meeting_report(runs, "<script>"))
        self.assertIn("&lt;script&gt;", meeting_report(runs[:1], "<script>"))
        job = ExperimentJob(self.store, self.dataset["id"], ExperimentConfig(tolerance_seconds=.05))
        job.run()
        with self.assertRaises(ValueError):
            load_comparison(self.store, [state["runs"][0], job.snapshot()["id"]])
        with self.assertRaises(RuntimeError):
            batch.start()

    def test_comparison_empty_truth_and_gap_policy(self):
        empty = pd.DataFrame(columns=["selection", "segment", "span", "region", "truth_time", "kind"])
        self.assertTrue(paired_cases(empty, empty).empty)
        a = ExperimentJob(self.store, self.dataset["id"])
        a.run()
        b = ExperimentJob(self.store, self.dataset["id"], ExperimentConfig(algorithm=AlgorithmConfig(max_gap=.6)))
        b.run()
        with self.assertRaises(ValueError):
            load_comparison(self.store, [a.snapshot()["id"], b.snapshot()["id"]])

    def test_failed_algorithm_does_not_abort_batch(self):
        from infra import experiments
        real = experiments.detect
        def broken(records, config, algorithm_id, params):
            if algorithm_id == "adaptive-amplitude-v1":
                raise ValueError("deliberate synthetic failure")
            return real(records, config, algorithm_id, params)
        with patch.object(experiments, "detect", side_effect=broken):
            batch = BatchJob(self.store, self.dataset["id"], [ExperimentConfig(algorithm_id="adaptive-amplitude-v1"), ExperimentConfig(algorithm_id="hysteresis-v1")]).start()
            batch.thread.join(30)
        self.assertEqual(batch.snapshot()["status"], "completed_with_errors")
        self.assertEqual(len(batch.snapshot()["runs"]), 2)
        self.assertEqual(read_json(self.store.object_path("run", batch.snapshot()["runs"][1])/"manifest.json")["status"], "completed")

    def test_lab_ui_runs_and_compares(self):
        from beatdance.paths import PROJECT_ROOT
        import os
        with patch.dict(os.environ, {"BEATDANCE_INFRA_ROOT": self.tmp.name}):
            app = AppTest.from_file(str(PROJECT_ROOT / "infra_app.py"), default_timeout=30).run()
            app.sidebar.radio[0].set_value("算法实验台").run()
            self.assertFalse(app.exception)
            next(b for b in app.button if b.label == "批量运行").click().run()
            batch = app.session_state["counting_batch"]
            batch.thread.join(60)
            app.run()
            self.assertEqual(batch.snapshot()["status"], "completed")
            self.assertFalse(app.exception)
            self.assertFalse(app.error)
            self.assertTrue(any(b.label == "下载视频与算法对比页" for b in app.get("download_button")))
            self.assertTrue(any(b.label == "下载对比表" for b in app.get("download_button")))
            next(b for b in app.button if b.label == "生成组会报告").click().run()
            self.assertFalse(app.exception)
            self.assertTrue(any(b.label == "下载独立 HTML 报告" for b in app.get("download_button")))

if __name__ == "__main__":
    unittest.main()
