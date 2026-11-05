"""Infra contracts, reproducible experiments and five-page Streamlit regression."""

import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import numpy as np
import pandas as pd

from beatdance.paths import PROJECT_ROOT
from beatdance.signal_processing import AlgorithmConfig, analyze_acceleration, analyze_imu
from infra.demo import install_demo, synthetic_fixture
from infra.evaluation import evaluate_events
from infra.experiments import BusyError, ExperimentConfig, ExperimentJob, export_run
from infra.store import Store, read_json, validate_capture, write_json


class InfraCase(unittest.TestCase):
    def setUp(self):
        (PROJECT_ROOT / ".tmp").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=PROJECT_ROOT / ".tmp")
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)

    def demo(self):
        return install_demo(self.store)


class ContractTests(InfraCase):
    def test_static_synthetic_example_matches_generator(self):
        directory = PROJECT_ROOT / "data" / "examples" / "infra_synthetic"
        raw, meta, events, intervals = synthetic_fixture()
        self.assertEqual((directory / "imu_raw.csv").read_bytes(), raw)
        self.assertEqual(read_json(directory / "session.json"), meta)
        truth = read_json(directory / "ground_truth.json")
        self.assertEqual(truth["events"], events)
        self.assertEqual(truth["intervals"], intervals)
        self.assertEqual(len(validate_capture(raw, meta)), 775)

    def test_config_defaults_are_identical_and_invalid_values_rejected(self):
        times = np.arange(0, 10, 0.02)
        values = 9.81 + 1.2 * np.sin(4 * np.pi * times)
        pd.testing.assert_frame_equal(analyze_acceleration(times, values),
                                      analyze_acceleration(times, values, config=AlgorithmConfig()))
        self.assertTrue(analyze_imu(pd.DataFrame(), config=AlgorithmConfig()).empty)
        for kwargs in ({"signal": "bad"}, {"max_gap": float("nan")}, {"prominence": True},
                       {"cutoff_hz": -1}, {"min_peak_interval": 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                AlgorithmConfig(**kwargs)

    def test_import_validates_and_preserves_raw_bytes(self):
        raw, meta, _, _ = synthetic_fixture()
        key = self.store.import_capture(raw, meta)
        frame, imported, _ = self.store.capture(key)
        self.assertEqual(len(frame), 775)
        self.assertEqual(frame.segment.nunique(), 2)
        self.assertEqual((self.store.capture_path(key) / "imu_raw.csv").read_bytes(), raw)
        self.assertEqual(imported["source_capture_id"], "synthetic-fixture-v1")
        self.assertNotEqual(key, self.store.import_capture(raw, meta))
        with self.assertRaises(ValueError):
            self.store.import_capture(raw, {**meta, "status": "recording"})
        for bad in (raw.replace(b"127.0.0.1", b"not-an-ip", 1), raw.replace(b"0x70", b"0x99", 1),
                    raw.replace(b"seq,t_ms", b"unknown,t_ms", 1), raw + b"incomplete\n"):
            with self.subTest(), self.assertRaises(ValueError):
                self.store.import_capture(bad, meta)
        for bad in ("../outside", "2026/../../outside", "C:/outside", "date\\key"):
            with self.assertRaises(ValueError):
                self.store.capture_path(bad)

    def test_flat_capture_preserves_bytes_and_recovers_time_across_wrap(self):
        _, meta, _, _ = synthetic_fixture()
        meta["owner"] = "127.0.0.1"
        header = "host_s,seg,time,seq,t_us,t_ms,ax,ay,az,gx,gy,gz"
        rows = [
            "10,0,0,4294967295,4294967000,4294967,0,0,1,0,0,0",
            "11,0,0.001,0,704,0,0,0,1,0,0,0",
            "15,1,0,0,500,0,0,0,1,0,0,0",
        ]
        for has_time in (True, False):
            with self.subTest(has_time=has_time):
                lines = [header, *rows]
                if not has_time:
                    lines = [",".join(line.split(",")[:2] + line.split(",")[3:]) for line in lines]
                raw = ("\n".join(lines) + "\n").encode("ascii")
                key = self.store.import_capture(raw, meta)
                frame, _, _ = self.store.capture(key)
                self.assertEqual(frame.segment.tolist(), [0, 0, 1])
                self.assertEqual(frame.time.tolist(), [0.0, 0.001, 0.0])
                self.assertEqual(frame.az_g.tolist(), [1, 1, 1])
                self.assertEqual((self.store.capture_path(key) / "imu_raw.csv").read_bytes(), raw)
        for bad_row in (rows[0], rows[1].replace("0.001", "0.002")):
            bad = ("\n".join([header, rows[0], bad_row]) + "\n").encode("ascii")
            with self.subTest(bad_row=bad_row), self.assertRaises(ValueError):
                validate_capture(bad, meta)

    def test_annotation_revisions_and_dataset_snapshots(self):
        key, first, dataset = self.demo()
        old_bytes = self.store.object_path("annotation", first["id"]).read_bytes()
        moved = [{**e, "time": e["time"] + 0.01} for e in first["events"][:-1]]
        second = self.store.save_annotation(key, moved, first["intervals"], "edited", first["id"])
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(self.store.object_path("annotation", first["id"]).read_bytes(), old_bytes)
        saved = read_json(self.store.object_path("dataset", dataset["id"]))
        self.assertEqual(saved["items"][0]["annotation_id"], first["id"])
        self.assertEqual(second["parent"], first["id"])
        for events, intervals in (([{"segment": 8, "time": 1}], []),
                                  ([{"segment": 0, "time": 1}] * 2, []),
                                  ([], [{"segment": 0, "start": 2, "end": 1, "kind": "valid"}])):
            with self.assertRaises(ValueError):
                self.store.save_annotation(key, events, intervals)
        with self.assertRaises(ValueError):
            self.store.save_dataset("overlap", [saved["items"][0]] * 2)


class EvaluationTests(unittest.TestCase):
    def evaluate(self, predictions, truth, intervals=None, spans=None, **kwargs):
        annotation = {"events": [{"segment": 0, "time": t} for t in truth],
                      "intervals": intervals if intervals is not None else [
                          {"segment": 0, "start": 0, "end": 10, "kind": "valid"}]}
        return evaluate_events([{"span": s, "time": t} for s, t in predictions], annotation,
            segment=0, start=0, end=10, spans=spans or [(0, 0, 10)], **kwargs)

    def test_one_to_one_tolerance_boundary_and_error_counts(self):
        metrics, cases = self.evaluate([(0, 1.1), (0, 1.1), (0, 5)], [1, 3], tolerance=0.1)
        self.assertEqual((metrics["matched"], metrics["false_positive"], metrics["false_negative"]), (1, 2, 1))
        self.assertEqual(metrics["count_absolute_error"], 1)
        self.assertAlmostEqual(metrics["mean_signed_offset_seconds"], 0.1)
        self.assertEqual(len(cases), 4)

    def test_zero_truth_and_no_annotation(self):
        metrics, _ = self.evaluate([(0, 1)], [])
        self.assertEqual(metrics["false_positive"], 1)
        self.assertIsNone(metrics["count_error_rate"])
        self.assertIsNone(metrics["recall"])
        metrics, _ = evaluate_events([], None, segment=0, start=0, end=1, spans=[(0, 0, 1)])
        self.assertEqual(metrics["status"], "not_evaluated")
        metrics, _ = self.evaluate([], [1], intervals=[])
        self.assertEqual(metrics["status"], "not_evaluated")

    def test_exclusions_and_gaps_are_matching_barriers(self):
        intervals = [{"segment": 0, "start": 0, "end": 10, "kind": "valid"},
                     {"segment": 0, "start": 2, "end": 3, "kind": "exclude"}]
        metrics, _ = self.evaluate([(0, 2), (0, 3), (0, 3.05)], [1.95, 2.5], intervals, tolerance=2)
        self.assertEqual(metrics["matched"], 0)
        self.assertEqual(metrics["false_positive"], 1)
        self.assertEqual(metrics["false_negative"], 1)
        self.assertAlmostEqual(metrics["excluded_seconds"], 1)
        metrics, _ = self.evaluate([(1, 3.05)], [1.95], spans=[(0, 0, 2), (1, 3, 10)], tolerance=2)
        self.assertEqual(metrics["matched"], 0)

    def test_overlapping_valid_ranges_are_scored_once(self):
        intervals = [{"segment": 0, "start": 0, "end": 5, "kind": "valid"},
                     {"segment": 0, "start": 2, "end": 10, "kind": "valid"}]
        metrics, _ = self.evaluate([(0, 3)], [3], intervals)
        self.assertEqual(metrics["matched"], 1)
        self.assertAlmostEqual(metrics["evaluated_seconds"], 10)


class ExperimentTests(InfraCase):
    def test_complete_reproducible_multisegment_run_and_export(self):
        key, _, dataset = self.demo()
        raw_before = (self.store.capture_path(key) / "imu_raw.csv").read_bytes()
        jobs = [ExperimentJob(self.store, dataset["id"]) for _ in range(2)]
        for job in jobs:
            self.assertEqual(job.run()["status"], "completed")
        self.assertNotEqual(jobs[0].directory, jobs[1].directory)
        self.assertEqual((jobs[0].directory / "events.csv").read_bytes(), (jobs[1].directory / "events.csv").read_bytes())
        self.assertEqual(read_json(jobs[0].directory / "metrics.json"), read_json(jobs[1].directory / "metrics.json"))
        analysis = pd.read_csv(jobs[0].directory / "analysis.csv")
        self.assertEqual(analysis.segment.nunique(), 2)
        self.assertFalse(analysis[(analysis.segment == 0)].time.between(3.01, 3.49).any())
        self.assertEqual((self.store.capture_path(key) / "imu_raw.csv").read_bytes(), raw_before)
        manifest = jobs[0].snapshot()
        self.assertIn("dependencies", manifest["provenance"])
        self.assertIn("dataset_sha256", manifest)
        with zipfile.ZipFile(io.BytesIO(export_run(self.store, manifest["id"]))) as archive:
            self.assertIn("report.html", archive.namelist())
            self.assertIn("inputs/0/annotation.json", archive.namelist())
        with self.assertRaises(RuntimeError):
            jobs[0].run()

    def test_mutated_capture_or_annotation_is_failed_not_silently_reused(self):
        key, annotation, dataset = self.demo()
        path = self.store.object_path("annotation", annotation["id"])
        old = path.read_bytes()
        path.write_bytes(old + b"\n")
        job = ExperimentJob(self.store, dataset["id"])
        self.assertEqual(job.run()["status"], "failed")
        self.assertIn("annotation changed", job.snapshot()["error"])
        path.write_bytes(old)
        metadata = self.store.capture_path(key) / "session.json"
        metadata.write_bytes(metadata.read_bytes() + b"\n")
        job = ExperimentJob(self.store, dataset["id"])
        self.assertEqual(job.run()["status"], "failed")
        self.assertIn("capture changed", job.snapshot()["error"])
        self.assertTrue((job.directory / "error.txt").exists())

    def test_partial_cancel_and_worker_exclusion(self):
        _, _, dataset = self.demo()
        job = ExperimentJob(self.store, dataset["id"])
        original = analyze_imu
        def cancel_after_one(*args, **kwargs):
            job.cancel()
            return original(*args, **kwargs)
        with patch("infra.experiments.analyze_imu", side_effect=cancel_after_one):
            result = job.run()
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["completed"], 1)
        self.assertFalse(pd.read_csv(job.directory / "events.csv").empty)
        held = ExperimentJob(self.store, dataset["id"])
        held._prepare()
        try:
            with self.assertRaises(BusyError):
                ExperimentJob(self.store, dataset["id"]).run()
        finally:
            held.cancel()
            held._execute()
        self.assertEqual(ExperimentJob(self.store, dataset["id"]).run()["status"], "completed")

    def test_unlabelled_selection_and_worker_crash_recovery(self):
        key, _, _ = self.demo()
        dataset = self.store.save_dataset("unlabelled", [{"capture_key": key, "segment": 0, "start": 1, "end": 2}])
        job = ExperimentJob(self.store, dataset["id"])
        job.run()
        self.assertEqual(read_json(job.directory / "metrics.json")["overall"]["status"], "not_evaluated")
        old = job.snapshot()
        old["status"] = "running"
        write_json(job.directory / "manifest.json", old)
        self.assertEqual(ExperimentJob(self.store, dataset["id"]).run()["status"], "completed")
        self.assertEqual(read_json(job.directory / "manifest.json")["status"], "interrupted")

    def test_cli_uses_same_runner(self):
        _, _, dataset = self.demo()
        result = subprocess.run([sys.executable, "-m", "infra.experiments", "--root", str(self.store.root),
                                 "run", "--dataset", dataset["id"]], cwd=PROJECT_ROOT,
                                capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "completed")


class WorkbenchTests(InfraCase):
    def app(self):
        from streamlit.testing.v1 import AppTest
        self.env = patch.dict("os.environ", {"BEATDANCE_INFRA_ROOT": str(self.store.root)})
        self.env.start()
        self.addCleanup(self.env.stop)
        app = AppTest.from_file(str(PROJECT_ROOT / "infra_app.py"), default_timeout=20)
        app.run()
        self.assertFalse(app.exception)
        self.assertFalse(app.error)
        return app

    def test_all_five_pages_and_annotation_revision_save(self):
        key, annotation, dataset = self.demo()
        app = self.app()
        for page in ("数据", "标注", "实验", "对比", "采集"):
            app.sidebar.radio[0].set_value(page).run()
            self.assertFalse(app.exception, page)
            self.assertFalse(app.error, page)
        app.sidebar.radio[0].set_value("标注").run()
        app.selectbox[1].set_value(annotation["id"]).run()
        self.assertFalse(app.exception)
        self.assertFalse(app.error)
        button = next(b for b in app.button if b.label == "保存为新标注版本")
        button.click().run()
        self.assertFalse(app.error)
        self.assertEqual(len(self.store.list_objects("annotation")), 2)

    def test_ui_experiment_does_not_restart_on_refresh_and_compares_runs(self):
        _, _, dataset = self.demo()
        app = self.app()
        app.sidebar.radio[0].set_value("实验").run()
        next(b for b in app.button if b.label == "运行实验").click().run()
        job = app.session_state["infra_job"]
        job.thread.join(timeout=20)
        self.assertFalse(job.thread.is_alive())
        app.run()
        app.run()
        self.assertEqual(len(self.store.list_objects("run")), 1)
        self.assertFalse(app.exception)
        self.assertFalse(app.error)
        second = ExperimentJob(self.store, dataset["id"], ExperimentConfig(AlgorithmConfig(prominence=10))).run()
        app.sidebar.radio[0].set_value("对比").run()
        app.multiselect[0].set_value([job.snapshot()["id"], second["id"]]).run()
        self.assertFalse(app.exception)
        self.assertFalse(app.error)
        self.assertTrue(any(b.label == "下载 report.html" for b in app.get("download_button")))

    def test_switching_page_stops_receiver_and_preserves_csv(self):
        app = self.app()
        next(n for n in app.number_input if n.label == "UDP 端口").set_value(0)
        next(b for b in app.button if b.label == "开始采集").click().run()
        receiver = app.session_state["infra_receiver"]
        self.addCleanup(receiver.stop)
        self.assertTrue(receiver.snapshot(read_rows=False)[1]["running"])
        app.sidebar.radio[0].set_value("数据").run()
        self.assertFalse(receiver.snapshot(read_rows=False)[1]["running"])
        self.assertTrue(receiver.csv_path.exists())
        self.assertFalse(app.exception)

    def test_ui_creates_dataset_runs_two_configs_and_locates_failure(self):
        app = self.app()
        app.sidebar.radio[0].set_value("数据").run()
        next(b for b in app.button if b.label == "创建合成流程示例").click().run()
        self.assertFalse(app.exception)
        self.assertFalse(app.error)
        annotation = self.store.list_objects("annotation")[0]
        next(s for s in app.selectbox if s.label == "固定引用的标注版本").set_value(annotation["id"]).run()
        next(b for b in app.button if b.label == "将当前片段加入数据集草稿").click().run()
        next(t for t in app.text_input if t.label == "数据集名称").set_value("UI full workflow")
        next(b for b in app.button if b.label == "保存数据集快照").click().run()
        dataset = next(d for d in self.store.list_objects("dataset") if d["name"] == "UI full workflow")
        self.assertEqual(len(dataset["items"]), 1)
        app.sidebar.radio[0].set_value("实验").run()
        next(s for s in app.selectbox if s.label == "数据集快照").set_value(dataset["id"])
        next(b for b in app.button if b.label == "运行实验").click().run()
        first = app.session_state["infra_job"]
        first.thread.join(timeout=20)
        app.run()
        next(n for n in app.number_input if n.label == "峰显著度 / m/s²").set_value(10.0)
        next(b for b in app.button if b.label == "运行实验").click().run()
        second = app.session_state["infra_job"]
        second.thread.join(timeout=20)
        app.run()
        self.assertEqual(second.snapshot()["status"], "completed")
        self.assertEqual(read_json(second.directory / "config.json")["algorithm"]["prominence"], 10)
        self.assertEqual(len(self.store.list_objects("run")), 2)
        app.sidebar.radio[0].set_value("对比").run()
        app.multiselect[0].set_value([first.snapshot()["id"], second.snapshot()["id"]]).run()
        failure_selector = next(s for s in app.selectbox if s.key.startswith(f"failure_{second.snapshot()['id']}"))
        failure_selector.set_value(0).run()
        self.assertFalse(app.exception)
        self.assertFalse(app.error)
        self.assertTrue(any(b.label == "导出实验包" for b in app.get("download_button")))

    def test_comparison_remains_available_when_source_is_missing(self):
        key, _, dataset = self.demo()
        job = ExperimentJob(self.store, dataset["id"])
        job.run()
        # Move only this test's own synthetic input inside its temporary workspace.
        source = self.store.capture_path(key) / "imu_raw.csv"
        source.rename(source.with_suffix(".backup"))
        app = self.app()
        app.sidebar.radio[0].set_value("对比").run()
        self.assertFalse(app.exception)
        self.assertFalse(app.error)
        self.assertTrue(app.warning)
        self.assertTrue(any(b.label == "下载 report.html" for b in app.get("download_button")))


if __name__ == "__main__":
    unittest.main()
