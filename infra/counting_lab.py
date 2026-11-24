"""Batch orchestration, paired case review and self-contained meeting reports."""
import argparse
import base64
import html
import io
import json
import threading
from pathlib import Path
import matplotlib.pyplot as plt
import pandas as pd
from .experiments import ExperimentConfig, ExperimentJob
from .store import read_json, write_json, new_id, utc_now, Store


def paired_cases(left, right):
    """Join scored truth events by exact immutable identity; don't fuzzy-join errors."""
    keys = ["selection", "segment", "span", "region", "truth_time"]
    a = left[left.truth_time.notna()][keys + ["kind"]]
    b = right[right.truth_time.notna()][keys + ["kind"]]
    result = a.merge(b, on=keys, suffixes=("_A", "_B"), validate="one_to_one")
    if result.empty:
        result["change"] = pd.Series(dtype=str)
        return result
    result["change"] = result.apply(lambda r: "B recovered" if r.kind_A == "false_negative" and r.kind_B == "matched"
        else "B missed" if r.kind_A == "matched" and r.kind_B == "false_negative" else "both missed"
        if r.kind_A == r.kind_B == "false_negative" else "both matched", axis=1)
    return result


def load_comparison(store, run_ids):
    runs = []
    for run_id in run_ids:
        directory = store.object_path("run", run_id)
        manifest = read_json(directory / "manifest.json")
        if manifest["status"] != "completed":
            raise ValueError("仅可比较成功完成的实验")
        config = read_json(directory / "config.json")
        runs.append(dict(id=run_id, directory=directory, manifest=manifest, config=config,
                         metrics=read_json(directory / "metrics.json"),
                         analysis=pd.read_csv(directory / "analysis.csv"),
                         cases=pd.read_csv(directory / "cases.csv")))
    if not runs:
        raise ValueError("请选择实验")
    signatures = {(r["manifest"]["dataset_sha256"], r["config"]["tolerance_seconds"],
                   r["manifest"]["matching_policy"], r["config"]["algorithm"].get("max_gap", 0.2)) for r in runs}
    if len(signatures) != 1:
        raise ValueError("配对对比要求相同数据快照、标注、缺口分段、匹配容差和评分规则")
    return runs


def summary_rows(runs):
    return [{"run": r["id"], "algorithm": r["config"]["algorithm_id"],
             "compute_seconds": sum(s.get("compute_seconds", 0) for s in r["manifest"].get("timings", [])),
             **r["metrics"]["overall"]} for r in runs]


def overlay(runs, selection, start, end):
    fig, axes = plt.subplots(len(runs)+1, 1, figsize=(12, 2+1.8*len(runs)), sharex=True, squeeze=False)
    first = runs[0]["analysis"]
    part = first[(first.selection == selection) & first.time.between(start, end)]
    for _, group in part.groupby("span"):
        axes[0, 0].plot(group.time, group.raw_acceleration, color="steelblue")
    axes[0, 0].set_ylabel("Raw m/s2")
    annotation = runs[0]["directory"] / "inputs" / str(selection) / "annotation.json"
    if annotation.exists():
        item = read_json(runs[0]["directory"] / "dataset.json")["items"][selection]
        for event in read_json(annotation)["events"]:
            if event["segment"] == item["segment"] and start <= event["time"] <= end:
                for ax in axes[:, 0]:
                    ax.axvline(event["time"], color="green", alpha=0.35)
    for ax, run in zip(axes[1:, 0], runs):
        a = run["analysis"]
        a = a[(a.selection == selection) & a.time.between(start, end)]
        for _, group in a.groupby("span"):
            ax.plot(group.time, group.filtered_acceleration, color="royalblue")
            for name in ("high_threshold", "low_threshold"):
                if name in group:
                    ax.plot(group.time, group[name], linestyle="--", alpha=0.6)
        peaks = a[a.is_peak == 1]
        ax.scatter(peaks.time, peaks.filtered_acceleration, color="red", s=18)
        ax.set_ylabel(run["config"]["algorithm_id"].replace("-v1", ""), fontsize=7)
    axes[-1, 0].set(xlabel="Device time (s)", xlim=(start, max(end, start+0.01)))
    fig.tight_layout()
    return fig


def meeting_report(runs, question="", conclusion=""):
    fragments = ["<!doctype html><meta charset='utf-8'><title>Counting comparison</title>",
        "<style>body{font:15px sans-serif;margin:32px}table{border-collapse:collapse}td,th{padding:6px;border:1px solid #ccc}img{max-width:100%}</style>",
        "<h1>离线计数算法对比</h1><p>"+html.escape(question)+"</p>",
        "<p>仅为离线实验；合成数据不代表真实精度。耗时为计算耗时，不是在线延迟。绿线为人工动作，红点为检测事件。</p>",
        pd.DataFrame(summary_rows(runs)).to_html(index=False),
        "<h2>结论与已知限制</h2><p>"+html.escape(conclusion)+"</p>"]
    for run in runs:
        fragments += ["<h2>"+html.escape(run["id"])+"</h2>", "<pre>"+html.escape(json.dumps(run["config"], ensure_ascii=False, indent=2))+"</pre>",
                      pd.DataFrame(run["metrics"]["selections"]).to_html(index=False)]
    # Bounded overview; full cases remain downloadable separately.
    for selection in list(runs[0]["analysis"].selection.unique())[:6]:
        part = runs[0]["analysis"].query("selection == @selection")
        if part.empty:
            continue
        fig = overlay(runs, int(selection), float(part.time.min()), float(part.time.max()))
        buffer = io.BytesIO()
        fig.savefig(buffer, format="png", dpi=120)
        plt.close(fig)
        fragments.append("<h2>Selection "+str(selection)+"</h2><img src='data:image/png;base64,"+base64.b64encode(buffer.getvalue()).decode()+"'>")
    if len(runs) >= 2:
        fragments += ["<h2>A/B 人工事件配对差异</h2>", paired_cases(runs[0]["cases"], runs[1]["cases"]).to_html(index=False)]
    fragments.append("<p>图示最多包含前六个片段；完整评分见上表。输入与来源以各实验清单为准。</p>")
    return "\n".join(fragments)


class BatchJob:
    """One background queue; existing ExperimentJob owns each run's OS lock."""
    def __init__(self, store, dataset_id, configs, question="", purpose="development"):
        if not configs or len(configs) > 64:
            raise ValueError("每批实验需要 1–64 个配置")
        for config in configs:
            ExperimentConfig.from_dict(config.to_dict())
        self.store, self.dataset_id, self.configs = store, dataset_id, configs
        self.directory = store.runs.parent / "counting_batches" / new_id("batch")
        self.directory.mkdir(parents=True, exist_ok=False)
        self.state = dict(id=self.directory.name, dataset_id=dataset_id, question=question,
            purpose=purpose, status="queued", created_at_utc=utc_now(), total=len(configs), runs=[], errors=[])
        self.cancel_event = threading.Event()
        self.thread = None
        self.current = None
        self.started = False
        write_json(self.directory / "batch.json", self.state)
        write_json(self.directory / "configs.json", [c.to_dict() for c in configs])

    def start(self):
        if self.started:
            raise RuntimeError("batch already started")
        self.started = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        return self

    def cancel(self):
        self.cancel_event.set()
        if self.current:
            self.current.cancel()

    def snapshot(self):
        return read_json(self.directory / "batch.json")

    def _run(self):
        self.state["status"] = "running"
        try:
            for config in self.configs:
                if self.cancel_event.is_set():
                    break
                try:
                    self.current = ExperimentJob(self.store, self.dataset_id, config)
                    result = self.current.run()
                    self.state["runs"].append(result["id"])
                    if result["status"] != "completed":
                        self.state["errors"].append(dict(algorithm=config.algorithm_id, error=result["error"], status=result["status"]))
                except Exception as exc:
                    self.state["errors"].append(dict(algorithm=config.algorithm_id, error=str(exc)))
                finally:
                    self.current = None
                    write_json(self.directory / "batch.json", self.state)
            self.state["status"] = "cancelled" if self.cancel_event.is_set() else "completed_with_errors" if self.state["errors"] else "completed"
        except Exception as exc:
            self.state.update(status="failed", fatal_error=str(exc))
        finally:
            self.state["ended_at_utc"] = utc_now()
            write_json(self.directory / "batch.json", self.state)


def main():
    parser = argparse.ArgumentParser(description="Batch replay with the Loss Longer counting algorithm")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--question", default="")
    args = parser.parse_args()
    job = BatchJob(Store(args.root), args.dataset, [ExperimentConfig()], args.question).start()
    job.thread.join()
    print(json.dumps(job.snapshot(), ensure_ascii=False, indent=2))
    if job.snapshot()["status"] != "completed":
        raise SystemExit(1)

if __name__ == "__main__":
    main()
