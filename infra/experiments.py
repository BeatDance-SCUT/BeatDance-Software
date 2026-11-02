"""Headless, cancellable offline experiments shared by CLI and workbench."""

import argparse
from dataclasses import dataclass, field
import html
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import subprocess
import threading
import traceback
import zipfile
import io

import pandas as pd

from beatdance.paths import PROJECT_ROOT
from beatdance.signal_processing import AlgorithmConfig, analyze_imu
from .evaluation import evaluate_events, summarize
from .store import Store, digest, new_id, number, read_json, utc_now, write_json


ALGORITHM_ID = "offline-butterworth-peaks-v1"
ANALYSIS_COLUMNS = ["selection", "capture_key", "signal", "segment", "time", "raw_acceleration",
                    "filtered_acceleration", "is_peak", "span"]
CASE_COLUMNS = ["selection", "capture_key", "segment", "span", "region", "kind", "predicted_time",
                "truth_time", "time", "offset_seconds"]


@dataclass(frozen=True)
class ExperimentConfig:
    algorithm: AlgorithmConfig = field(default_factory=AlgorithmConfig)
    tolerance_seconds: float = 0.1

    def __post_init__(self):
        if (isinstance(self.tolerance_seconds, bool) or not isinstance(self.tolerance_seconds, (int, float))
                or number(self.tolerance_seconds, "tolerance") <= 0):
            raise ValueError("tolerance must be positive")

    def to_dict(self):
        return {"algorithm_id": ALGORITHM_ID, "algorithm": self.algorithm.to_dict(),
                "tolerance_seconds": self.tolerance_seconds}

    @classmethod
    def from_dict(cls, value):
        if set(value) - {"algorithm_id", "algorithm", "tolerance_seconds"}:
            raise ValueError("unknown experiment configuration field")
        if value.get("algorithm_id", ALGORITHM_ID) != ALGORITHM_ID:
            raise ValueError("unsupported algorithm")
        return cls(AlgorithmConfig(**value.get("algorithm", {})), value.get("tolerance_seconds", 0.1))


class BusyError(RuntimeError):
    pass


class WorkerLock:
    """OS file lock: CLI and UI share one worker; process death releases it."""

    def __init__(self, store):
        store.runs.mkdir(parents=True, exist_ok=True)
        self.stream = (store.runs / ".worker.lock").open("a+b")
        self.stream.seek(0, os.SEEK_END)
        if self.stream.tell() == 0:
            self.stream.write(b"0")
            self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.stream.close()
            raise BusyError("another experiment is running") from exc

    def close(self):
        if not self.stream.closed:
            if os.name == "nt":
                import msvcrt
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            self.stream.close()


def capture_provenance(directory):
    """Record local software code and environment; no hardware credentials."""
    directory = Path(directory)
    result = {"python": platform.python_version(), "platform": platform.platform(),
              "dependencies": {p: version(p) for p in ("numpy", "pandas", "scipy", "matplotlib", "streamlit")}}
    def git(*args):
        return subprocess.run(["git", "-C", str(PROJECT_ROOT), *args], capture_output=True, check=True).stdout
    try:
        result["commit"] = git("rev-parse", "HEAD").decode().strip()
        diff = git("diff", "HEAD", "--binary")
        (directory / "code.diff").write_bytes(diff)
        result["diff_sha256"] = digest(diff)
        untracked = []
        for raw in git("ls-files", "--others", "--exclude-standard", "-z").split(b"\0"):
            if not raw:
                continue
            relative = Path(os.fsdecode(raw))
            path = PROJECT_ROOT / relative
            if relative.suffix == ".py" and path.resolve().is_relative_to(PROJECT_ROOT.resolve()):
                content = path.read_bytes()
                target = directory / "source" / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                untracked.append({"path": relative.as_posix(), "sha256": digest(content)})
        result["untracked_python"] = untracked
        result["dirty"] = bool(diff or untracked)
    except (OSError, subprocess.CalledProcessError) as exc:
        result.update(commit=None, provenance_error=str(exc))
    return result


def report_html(manifest, config, metrics, cases):
    def block(value):
        return "<pre>" + html.escape(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)) + "</pre>"
    rows = "".join("<tr>" + "".join(f"<td>{html.escape(str(c.get(k, '')))}</td>"
                                    for k in CASE_COLUMNS) + "</tr>" for c in cases)
    header = "".join(f"<th>{k}</th>" for k in CASE_COLUMNS)
    return ("<!doctype html><html lang='zh'><meta charset='utf-8'><title>BeatDance 实验报告</title>"
            "<style>body{font:15px system-ui;margin:32px}pre{white-space:pre-wrap}"
            "table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:5px}</style>"
            "<h1>BeatDance 离线实验报告</h1><p>候选波峰对人工标注；时间偏差不代表在线或声音延迟。</p>"
            "<h2>实验</h2>" + block(manifest) + "<h2>参数</h2>" + block(config)
            + "<h2>评估</h2>" + block(metrics) + f"<h2>事件匹配</h2><table><tr>{header}</tr>{rows}</table></html>")


class ExperimentJob:
    """One immutable-input run. start() is idempotence guarded; UI only polls."""

    def __init__(self, store, dataset_id, config=None):
        self.store, self.dataset_id = store, dataset_id
        self.config = config or ExperimentConfig()
        self.cancel_event = threading.Event()
        self.thread = None
        self.directory = None
        self.manifest = None
        self._started = False

    def _prepare(self):
        if self._started:
            raise RuntimeError("create a new job to rerun an experiment")
        self._lock = WorkerLock(self.store)
        try:
            # A free OS lock proves there is no live worker. Preserve crash records.
            for old in self.store.list_objects("run"):
                if old["status"] in ("queued", "running"):
                    old.update(status="interrupted", ended_at_utc=utc_now(), error="worker exited before finalization")
                    write_json(self.store.object_path("run", old["id"]) / "manifest.json", old)
            run_id = new_id("run")
            self.directory = self.store.object_path("run", run_id)
            self.directory.mkdir(exist_ok=False)
            self.manifest = {"schema_version": 1, "id": run_id, "dataset_id": self.dataset_id,
                             "algorithm_id": ALGORITHM_ID, "status": "queued", "created_at_utc": utc_now(),
                             "ended_at_utc": None, "completed": 0, "total": 0, "error": None,
                             "analysis_context": "whole source boot segment, split on gaps; crop outputs to selection",
                             "matching_policy": "chronological earliest feasible one-to-one, within inclusive tolerance"}
            write_json(self.directory / "config.json", self.config.to_dict())
            write_json(self.directory / "manifest.json", self.manifest)
            self._started = True
        except Exception:
            self._lock.close()
            raise

    def start(self):
        self._prepare()
        self.thread = threading.Thread(target=self._execute, name="beatdance-experiment", daemon=True)
        try:
            self.thread.start()
        except Exception:
            self._lock.close()
            raise
        return self

    def run(self):
        self._prepare()
        self._execute()
        return self.snapshot()

    def cancel(self):
        self.cancel_event.set()

    def snapshot(self):
        return read_json(self.directory / "manifest.json") if self.directory else None

    def _execute(self):
        analyses, peaks, cases, per_selection = [], [], [], []
        try:
            self.manifest.update(status="running", provenance=capture_provenance(self.directory))
            dataset_path = self.store.object_path("dataset", self.dataset_id)
            dataset_bytes = dataset_path.read_bytes()
            dataset = json.loads(dataset_bytes)
            self.manifest.update(dataset_sha256=digest(dataset_bytes), total=len(dataset["items"]))
            write_json(self.directory / "dataset.json", dataset)
            self._save_progress(analyses, peaks, cases, per_selection)
            for index, item in enumerate(dataset["items"]):
                if self.cancel_event.is_set():
                    break
                frame, meta, hashes = self.store.capture(item["capture_key"])
                if any(hashes[k] != item[k] for k in hashes):
                    raise ValueError(f"capture changed since dataset creation: {item['capture_key']}")
                annotation = None
                if item["annotation_id"]:
                    path = self.store.object_path("annotation", item["annotation_id"])
                    data = path.read_bytes()
                    if digest(data) != item["annotation_sha256"]:
                        raise ValueError("annotation changed since dataset creation")
                    annotation = json.loads(data)
                inputs = self.directory / "inputs" / str(index)
                inputs.mkdir(parents=True)
                write_json(inputs / "session.json", meta)
                if annotation:
                    write_json(inputs / "annotation.json", annotation)
                selected = frame[frame.segment == item["segment"]]
                # Analyze the whole source segment for stable offline filter context,
                # then crop outputs to the selection. No cross-gap filtering occurs.
                analysis = analyze_imu(selected, config=self.config.algorithm)
                analysis.insert(0, "segment", item["segment"])
                analysis.insert(0, "signal", self.config.algorithm.signal)
                spans = [(int(s), float(g.time.min()), float(g.time.max())) for s, g in analysis.groupby("span")]
                analysis = analysis[analysis.time.between(item["start"], item["end"])].copy()
                analysis.insert(0, "capture_key", item["capture_key"])
                analysis.insert(0, "selection", index)
                candidates = analysis[analysis.is_peak == 1].copy()
                metrics, matched = evaluate_events(candidates.to_dict("records"), annotation,
                    segment=item["segment"], start=item["start"], end=item["end"], spans=spans,
                    tolerance=self.config.tolerance_seconds)
                for case in matched:
                    case.update(selection=index, capture_key=item["capture_key"])
                analyses.append(analysis)
                peaks.append(candidates)
                cases.extend(matched)
                per_selection.append({"selection": index, "capture_key": item["capture_key"], **metrics})
                self.manifest["completed"] = index + 1
                self._save_progress(analyses, peaks, cases, per_selection)
            self.manifest["status"] = "cancelled" if self.cancel_event.is_set() else "completed"
        except KeyboardInterrupt:
            self.cancel_event.set()
            self.manifest["status"] = "cancelled"
        except Exception as exc:
            self.manifest.update(status="failed", error=f"{type(exc).__name__}: {exc}")
            (self.directory / "error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        finally:
            self.manifest["ended_at_utc"] = utc_now()
            try:
                self._save_progress(analyses, peaks, cases, per_selection, publish=False)
                metrics = read_json(self.directory / "metrics.json")
                (self.directory / "report.html").write_text(report_html(
                    self.manifest, self.config.to_dict(), metrics, cases), encoding="utf-8")
                write_json(self.directory / "manifest.json", self.manifest)
            finally:
                self._lock.close()

    def _save_progress(self, analyses, peaks, cases, per_selection, publish=True):
        for name, frames in (("analysis", analyses), ("events", peaks)):
            frame = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=ANALYSIS_COLUMNS)
            temp = self.directory / f"{name}.csv.tmp"
            frame.to_csv(temp, index=False)
            temp.replace(self.directory / f"{name}.csv")
        temp = self.directory / "cases.csv.tmp"
        pd.DataFrame(cases, columns=CASE_COLUMNS).to_csv(temp, index=False)
        temp.replace(self.directory / "cases.csv")
        evaluated = sum(m["status"] == "evaluated" for m in per_selection)
        status = "not_evaluated" if not evaluated else ("evaluated" if evaluated == len(per_selection) else "partial")
        overall = summarize(cases, sum(m["evaluated_seconds"] for m in per_selection),
                            sum(m["excluded_seconds"] for m in per_selection), status)
        # Keep per-selection absolute errors; opposite count errors must not cancel.
        overall["sum_selection_absolute_errors"] = sum(m["count_absolute_error"] for m in per_selection if m["status"] == "evaluated")
        write_json(self.directory / "metrics.json", {"overall": overall, "selections": per_selection})
        if publish:
            write_json(self.directory / "manifest.json", self.manifest)


def export_run(store, run_id):
    """Portable outputs and immutable manifests; raw inputs remain separately backed up."""
    directory = store.object_path("run", run_id)
    if read_json(directory / "manifest.json")["status"] in ("queued", "running"):
        raise ValueError("wait for the experiment to finish before exporting")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(directory.rglob("*")):
            if path.is_file() and not path.name.endswith(".tmp"):
                archive.write(path, path.relative_to(directory).as_posix())
    return buffer.getvalue()


def main():
    parser = argparse.ArgumentParser(description="BeatDance offline experiment runner")
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--dataset", required=True, help="immutable dataset id")
    run.add_argument("--config", type=Path, help="JSON ExperimentConfig; defaults if omitted")
    args = parser.parse_args()
    try:
        config = ExperimentConfig.from_dict(read_json(args.config)) if args.config else ExperimentConfig()
    except (OSError, ValueError, TypeError) as exc:
        parser.error(str(exc))
    job = ExperimentJob(Store(args.root), args.dataset, config)
    try:
        job.run()
    except BusyError as exc:
        parser.exit(2, f"{exc}\n")
    print(json.dumps(job.snapshot(), ensure_ascii=False, indent=2))
    return 0 if job.snapshot()["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
