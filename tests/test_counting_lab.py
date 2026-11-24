"""Synthetic contract checks, not evidence of real jump-rope accuracy."""
import json
import tempfile
import unittest
from pathlib import Path
import numpy as np
import pandas as pd
from streamlit.testing.v1 import AppTest
from unittest.mock import patch
from beatdance.counting import AXES, detect, new_counter
from infra.store import Store, read_json
from infra.demo import install_demo
from infra.experiments import ExperimentConfig, ExperimentJob
from infra.counting_lab import BatchJob, load_comparison, paired_cases, meeting_report

class DetectorTests(unittest.TestCase):
    def frame(self, amplitude=1.0):
        t = np.arange(0, 8, 0.02)
        phase = 2 * np.pi * t / .5
        return pd.DataFrame(dict(time=t, segment=0, ax_g=0., ay_g=0.,
                                 az_g=1+amplitude*np.sin(phase),
                                 gx_dps=150*amplitude*np.sin(phase),
                                 gy_dps=0., gz_dps=0.))

    def test_config_roundtrip_and_validation(self):
        config = ExperimentConfig(detector_params={"weak_score": .42})
        self.assertEqual(ExperimentConfig.from_dict(config.to_dict()), config)
        for invalid in ({"weak_score": 1}, {"interval_tolerance": 0}, {"bogus": 1},
                        {"allow_short_pair": 1}, {"hold_periods": float("nan")}):
            with self.assertRaises(ValueError):
                new_counter(invalid)

    def test_stream_prefix_and_batch_replay_publish_the_same_events(self):
        data = self.frame()
        packets = list(data[["time", *AXES]].itertuples(index=False, name=None))
        counter = new_counter()
        events = [event for packet in packets for event in counter.push(*packet)]
        assert events and all(e.emitted_s >= e.time_s for e in events)
        replay = detect(data).analysis.query("is_peak == 1")
        finished_events = events + list(counter.finish())
        np.testing.assert_array_equal(replay.time, [e.time_s for e in finished_events])
        np.testing.assert_array_equal(replay.emitted_s, [e.emitted_s for e in finished_events])
        earlier = new_counter()
        prefix = [event for packet in packets[:200] for event in earlier.push(*packet)]
        self.assertEqual(prefix, [e for e in events if e.emitted_s <= packets[199][0]])

    def test_periodic_static_and_gap_reset(self):
        self.assertGreater(detect(self.frame()).analysis.is_peak.sum(), 10)
        self.assertEqual(detect(self.frame(0)).analysis.is_peak.sum(), 0)
        data = self.frame()
        trace = detect(data[(data.time < 3) | (data.time > 4)]).analysis
        self.assertEqual(trace.span.nunique(), 2)
        self.assertFalse(trace.time.between(3, 4).any())


class LabTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        _, _, self.dataset = install_demo(self.store)

    def test_batch_compare_report_and_source_compatibility(self):
        batch = BatchJob(self.store, self.dataset["id"], [ExperimentConfig(), ExperimentConfig(detector_params={"weak_score": .42})], "test <script>").start()
        batch.thread.join(60)
        self.assertFalse(batch.thread.is_alive())
        state = batch.snapshot()
        self.assertEqual(state["status"], "completed", state)
        runs = load_comparison(self.store, state["runs"])
        self.assertEqual(len(runs), 2)
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

    def test_comparison_empty_truth_and_tolerance_policy(self):
        empty = pd.DataFrame(columns=["selection", "segment", "span", "region", "truth_time", "kind"])
        self.assertTrue(paired_cases(empty, empty).empty)
        a = ExperimentJob(self.store, self.dataset["id"])
        a.run()
        b = ExperimentJob(self.store, self.dataset["id"], ExperimentConfig(tolerance_seconds=.6))
        b.run()
        with self.assertRaises(ValueError):
            load_comparison(self.store, [a.snapshot()["id"], b.snapshot()["id"]])

    def test_failed_algorithm_does_not_abort_batch(self):
        from infra import experiments
        real = experiments.detect
        def broken(records, params):
            if params["weak_score"] == .42:
                raise ValueError("deliberate synthetic failure")
            return real(records, params)
        with patch.object(experiments, "detect", side_effect=broken):
            batch = BatchJob(self.store, self.dataset["id"], [ExperimentConfig(detector_params={"weak_score": .42}), ExperimentConfig()]).start()
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
