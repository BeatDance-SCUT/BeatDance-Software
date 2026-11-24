"""Review payload fidelity, escaping, and Streamlit page integration."""

import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from beatdance.paths import PROJECT_ROOT
from beatdance.counting import detect
from infra.demo import install_demo
from infra.store import Store
from infra.video_review import build_review_html, build_comparison_review_html


class VideoReviewTests(unittest.TestCase):
    def setUp(self):
        (PROJECT_ROOT / ".tmp").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=PROJECT_ROOT / ".tmp")
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.key, _, _ = install_demo(self.store)

    def test_payload_preserves_spans_peaks_and_segment(self):
        frame, _, hashes = self.store.capture(self.key)
        html = build_review_html(frame, "</script><script>alert(1)</script>", hashes["raw_sha256"], 0)
        data = json.loads(re.search(r'<script id="review-data" type="application/json">(.*?)</script>', html, re.S)[1])
        expected = detect(frame[frame.segment == 0]).analysis
        actual = pd.DataFrame([p for s in data["spans"] for p in s], columns=["time", "raw_acceleration", "filtered_acceleration", "is_peak"])
        pd.testing.assert_frame_equal(actual, expected[actual.columns], check_dtype=False)
        self.assertEqual(len(data["spans"]), expected.span.nunique())
        self.assertNotIn('</script><script>alert(1)', html)
        self.assertEqual(data["raw_sha256"], hashes["raw_sha256"])
        with self.assertRaisesRegex(ValueError, "没有样本"):
            build_review_html(frame, self.key, hashes["raw_sha256"], 99)

    def test_saved_comparison_payload_and_video_entry(self):
        from infra.experiments import ExperimentConfig, ExperimentJob
        from infra.counting_lab import load_comparison
        from streamlit.testing.v1 import AppTest
        dataset = self.store.list_objects("dataset")[0]
        ids = []
        for params in ({}, {"weak_score": .42}):
            job = ExperimentJob(self.store, dataset["id"], ExperimentConfig(detector_params=params))
            self.assertEqual(job.run()["status"], "completed")
            ids.append(job.snapshot()["id"])
        runs = load_comparison(self.store, ids)
        html = build_comparison_review_html(runs, 0, 2.0)
        data = json.loads(re.search(r'<script id="review-data" type="application/json">(.*?)</script>', html, re.S)[1])
        self.assertEqual(len(data["tracks"]), 2)
        self.assertEqual(data["review_context"]["selection"], 0)
        self.assertEqual(data["focus"], 2)
        self.assertTrue(data["truth"])
        first = runs[0]["analysis"]
        first = first[first.selection == 0]
        self.assertEqual(sum(p[3] for span in data["tracks"][0]["spans"] for p in span), first.is_peak.sum())
        with patch.dict("os.environ", {"BEATDANCE_INFRA_ROOT": str(self.store.root)}):
            app = AppTest.from_file(str(PROJECT_ROOT / "infra_app.py"), default_timeout=25).run()
            app.sidebar.radio[0].set_value("视频复核").run()
            next(r for r in app.radio if r.label == "复核内容").set_value("算法实验对比").run()
            app.multiselect[0].set_value(ids).run()
            self.assertFalse(app.exception)
            self.assertFalse(app.error)
            self.assertTrue(any(b.label == "下载视频与算法对比页" for b in app.get("download_button")))
            next(s for s in app.selectbox if s.label == "视频对比片段").set_value(1).run()
            self.assertFalse(app.error)

    def test_review_page_selects_capture_and_other_segment(self):
        from streamlit.testing.v1 import AppTest
        with patch.dict("os.environ", {"BEATDANCE_INFRA_ROOT": str(self.store.root)}):
            app = AppTest.from_file(str(PROJECT_ROOT / "infra_app.py"), default_timeout=25).run()
            app.sidebar.radio[0].set_value("视频复核").run()
            self.assertFalse(app.exception)
            self.assertFalse(app.error)
            next(s for s in app.selectbox if s.label == "复核启动片段").set_value(1).run()
            self.assertFalse(app.exception)
            self.assertFalse(app.error)
            self.assertTrue(any(b.label == "下载独立复核页 HTML" for b in app.get("download_button")))


if __name__ == "__main__":
    unittest.main()
