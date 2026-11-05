"""Five-page local research UI. Background workers never call Streamlit."""

import json
import os
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import streamlit as st

from beatdance.imu_udp import UDPReceiver
from beatdance.capture_clock import render_capture_clock
from beatdance.paths import PROJECT_ROOT
from beatdance.recordings import CANONICAL_ACCELERATION, CANONICAL_GYRO, rows_to_frame, save_analysis
from beatdance.signal_processing import AlgorithmConfig
from .demo import install_demo
from .experiments import BusyError, ExperimentConfig, ExperimentJob, export_run
from .store import Store, read_json


PAGES = ("采集", "数据", "标注", "实验", "对比")
EVENT_COLUMNS = ["segment", "time"]
INTERVAL_COLUMNS = ["segment", "start", "end", "kind"]


def notice(message):
    st.session_state.infra_notice = message


def stop_capture(receiver):
    receiver.stop()
    try:
        save_analysis(receiver.directory)
        notice(f"采集已结束，原始数据与派生分析已保存：{receiver.capture_id}")
    except (OSError, ValueError, KeyError) as exc:
        notice(f"采集已结束，原始 CSV 已保留；派生分析未生成：{exc}")


def capture_selector(store, key="capture_choice", closed=True):
    captures = store.captures()
    if closed:
        captures = [c for c in captures if c["metadata"].get("status") in ("stopped", "error") and not c["error"]]
    if not captures:
        st.info("暂无可用采集。请先采集、导入，或在数据页创建合成示例。")
        return None
    return st.selectbox("采集记录", [c["key"] for c in captures], key=key)


def time_window(frame, key, preferred=None):
    if frame.empty:
        st.info("该记录没有已保存样本。")
        return None
    segments = [int(s) for s in frame.segment.unique()]
    if preferred and preferred[0] in segments:
        st.session_state[f"{key}_segment"] = preferred[0]
    segment = st.selectbox("设备启动片段", segments, key=f"{key}_segment")
    part = frame[frame.segment == segment]
    low, high = float(part.time.min()), float(part.time.max())
    if preferred:
        st.session_state[f"{key}_start"] = max(low, preferred[1] - 1)
        st.session_state[f"{key}_end"] = min(high, preferred[1] + 1)
    # Changing capture/segment resets bounds, avoiding stale values from prior data.
    signature = (segment, low, high, len(part))
    if not preferred and (st.session_state.get(f"{key}_signature") != signature
                          or f"{key}_start" not in st.session_state or f"{key}_end" not in st.session_state):
        st.session_state[f"{key}_start"] = low
        st.session_state[f"{key}_end"] = min(high, low + 10)
    st.session_state[f"{key}_signature"] = signature
    left, right = st.columns(2)
    start = left.number_input("窗口起点 / s", min_value=low, max_value=high, value=None,
                              key=f"{key}_start", step=0.1, format="%.3f")
    end = right.number_input("窗口终点 / s", min_value=low, max_value=high, value=None,
                             key=f"{key}_end", step=0.1, format="%.3f")
    if start is None or end is None or end < start:
        st.warning("窗口终点必须大于或等于起点。")
        return None
    return segment, start, end


def raw_plot(frame, segment, start, end, annotation=None):
    part = frame[(frame["segment"] == segment) & frame["time"].between(start, end)]

    if part.empty:
        st.info("窗口内没有样本。")
        return

    cuts = np.flatnonzero(np.diff(part["time"]) > 0.2 + 1e-9) + 1

    fig, axes = plt.subplots(2, 1, sharex=True, figsize=(11, 4))

    for ax, columns, unit in (
        (axes[0], CANONICAL_ACCELERATION, "g"),
        (axes[1], CANONICAL_GYRO, "deg/s"),
    ):
        for index, block in enumerate(np.split(np.arange(len(part)), cuts)):
            display = part.iloc[block][::max(1, len(part) // 2000)]

            for column in columns:
                ax.plot(
                    display["time"],
                    display[column],
                    label=column if index == 0 else None,
                )

        ax.set_ylabel(unit)
        ax.legend(loc="upper right")

    if annotation:
        for event in annotation["events"]:
            if event["segment"] == segment and start <= event["time"] <= end:
                axes[0].axvline(event["time"], color="green", alpha=0.6)

        for interval in annotation["intervals"]:
            if (
                interval["segment"] == segment
                and interval["end"] >= start
                and interval["start"] <= end
            ):
                axes[0].axvspan(
                    max(start, interval["start"]),
                    min(end, interval["end"]),
                    color="green" if interval["kind"] == "valid" else "red",
                    alpha=0.12,
                )

    axes[1].set_xlim(start, max(end, start + 0.01))
    axes[1].set_xlabel("Device-relative time (s)")

    fig.tight_layout()
    st.pyplot(fig)
    plt.close(fig)

def capture_page(store):
    receiver = st.session_state.get("infra_receiver")
    running = receiver is not None and receiver.snapshot(read_rows=False)[1]["running"]
    with st.form("capture_settings"):
        bind = st.text_input("监听地址", "0.0.0.0", disabled=running)
        port = st.number_input("UDP 端口", min_value=0, max_value=65535, value=5005, disabled=running)
        participant = st.text_input("匿名参与者", disabled=running)
        activity = st.text_input("动作类型", "jump-rope", disabled=running)
        placement = st.text_input("佩戴位置与方向", disabled=running)
        notes = st.text_area("实验条件与备注", disabled=running)
        start = st.form_submit_button("开始采集", disabled=running)
    if start:
        new_receiver = UDPReceiver(bind, int(port), output_dir=store.recordings)
        try:
            new_receiver.set_labels(participant, activity, f"佩戴：{placement}\n{notes}")
            new_receiver.start()
        except Exception:
            new_receiver.stop()
            raise
        st.session_state.infra_receiver = new_receiver
        st.rerun()
    if receiver:
        if st.button("停止并保存", disabled=not running):
            stop_capture(receiver)
            st.rerun()
        st.caption("切换页面会结束采集；页面失联约 60 秒后释放端口。实时图为离线窗口分析的开发视图。")

        @st.fragment(run_every=0.5 if running else None)
        def live():
            rows, state = receiver.snapshot()
            render_capture_clock(rows, state, receiver.capture_id)
            st.json(state)
            if rows:
                frame = rows_to_frame(rows)
                segment = int(frame["segment"].iloc[-1])
                last = float(frame.loc[frame["segment"] == segment, "time"].max())
                raw_plot(frame, segment, max(0, last - 10), last)
            if state["error"]:
                st.error(state["error"])
        live()


def data_page(store):
    with st.expander("导入原始采集"):
        st.caption("只接收正式软件格式的 imu_raw.csv 和已结束的 session.json；不改写原文件。")
        raw = st.file_uploader("imu_raw.csv", type="csv", key="import_raw")
        session = st.file_uploader("session.json", type="json", key="import_session")
        if st.button("校验并导入", disabled=raw is None or session is None):
            key = store.import_capture(raw.getvalue(), json.loads(session.getvalue()))
            notice(f"已导入 {key}")
            st.rerun()
    if st.button("创建合成流程示例"):
        key, _, dataset = install_demo(store)
        notice(f"已创建合成数据 {key} 和数据集 {dataset['id']}；不代表真实识别精度。")
        st.rerun()
    query = st.text_input("筛选参与者、动作、佩戴或备注")
    captures = store.captures()
    filtered = [c for c in captures if query.lower() in json.dumps(c, ensure_ascii=False).lower()]
    st.dataframe(pd.DataFrame([{"记录": c["key"], "状态": c["metadata"].get("status"),
        "参与者": c["metadata"].get("labels", {}).get("participant"),
        "动作": c["metadata"].get("labels", {}).get("activity"),
        "备注": c["metadata"].get("labels", {}).get("notes"), "错误": c["error"]} for c in filtered]), hide_index=True)
    keys = [c["key"] for c in filtered if c["metadata"].get("status") in ("stopped", "error") and not c["error"]]
    key = st.selectbox("查看记录", keys) if keys else None
    if key:
        frame, meta, _ = store.capture(key)
        if meta.get("synthetic"):
            st.warning("合成数据，仅用于功能验收。")
        with st.expander("元信息与质量统计"):
            st.json(meta)
        window = time_window(frame, f"data_{key}")
        if window:
            raw_plot(frame, *window)
            with st.expander("时间窗回放"):
                st.caption("按设备相对时间推进窗口；用于浏览离线数据，不模拟网络和声音延迟。")
                speed = st.selectbox("回放速度", [1.0, 2.0, 5.0], key="replay_speed")
                playing = st.checkbox("播放", key=f"play_{key}")
                reset = st.button("回到窗口起点")
                cursor_key = f"cursor_{key}_{window[0]}"
                if reset or cursor_key not in st.session_state:
                    st.session_state[cursor_key] = window[1]

                @st.fragment(run_every=0.5 if playing else None)
                def replay():
                    cursor = st.session_state[cursor_key]
                    cursor = min(window[2], max(window[1], cursor) + (0.5 * speed if playing else 0))
                    st.session_state[cursor_key] = cursor
                    st.caption(f"当前设备时间：{cursor:.3f} s")
                    raw_plot(frame, window[0], max(window[1], cursor - 5), cursor)
                    if cursor >= window[2]:
                        st.info("已到达窗口末尾。")
                replay()
            annotations = [a for a in store.list_objects("annotation") if a["capture_key"] == key]
            annotation_id = st.selectbox("固定引用的标注版本", [None] + [a["id"] for a in annotations],
                                         format_func=lambda v: v or "无标注", key=f"dataset_annotation_{key}")
            if st.button("将当前片段加入数据集草稿"):
                item = {"capture_key": key, "segment": window[0], "start": window[1], "end": window[2],
                        "annotation_id": annotation_id}
                st.session_state.setdefault("dataset_draft", []).append(item)
                st.success("已加入草稿。")
        st.download_button("导出原始 CSV", (store.capture_path(key) / "imu_raw.csv").read_bytes(), "imu_raw.csv")
        st.download_button("导出采集元信息", json.dumps(meta, ensure_ascii=False, indent=2), "session.json")
    draft = st.session_state.get("dataset_draft", [])
    if draft:
        st.subheader("数据集草稿")
        st.dataframe(pd.DataFrame(draft), hide_index=True)
        name = st.text_input("数据集名称", "实验数据集")
        if st.button("保存数据集快照"):
            dataset = store.save_dataset(name, draft)
            st.session_state.dataset_draft = []
            notice(f"已保存数据集 {dataset['id']}")
            st.rerun()
        if st.button("清空草稿"):
            st.session_state.dataset_draft = []
            st.rerun()
    datasets = store.list_objects("dataset")
    if datasets:
        st.subheader("已保存数据集")
        st.dataframe(pd.DataFrame([{"id": d["id"], "名称": d["name"], "片段数": len(d["items"])} for d in datasets]), hide_index=True)
        selected = st.selectbox("导出数据集", [d["id"] for d in datasets])
        st.download_button("下载数据集清单", store.object_path("dataset", selected).read_bytes(), f"{selected}.json")
    with st.expander("旧分析 CSV 查看"):
        legacy = st.file_uploader("分析 CSV（不作为原始采集）", type="csv", key="legacy_csv")
        if legacy:
            frame = pd.read_csv(legacy)
            st.dataframe(frame.head(1000), hide_index=True)
            if {"time", "filtered_acceleration"}.issubset(frame.columns):
                st.line_chart(frame.set_index("time")[["filtered_acceleration"]])


def annotation_page(store):
    key = capture_selector(store, "annotation_capture")
    if key is None:
        return
    frame, _, _ = store.capture(key)
    revisions = [a for a in store.list_objects("annotation") if a["capture_key"] == key]
    revision_id = st.selectbox("加载标注版本", [None] + [a["id"] for a in revisions],
                               format_func=lambda v: v or "新建空白标注")
    old = next((a for a in revisions if a["id"] == revision_id), None)
    editor_key = f"annotation_{key}_{revision_id}"
    imported_key = f"{editor_key}_imported"
    upload = st.file_uploader("导入标注 JSON", type="json")
    if st.button("载入导入标注", disabled=upload is None):
        value = json.loads(upload.getvalue())
        if value.get("capture_key") != key:
            raise ValueError("标注的 capture_key 必须对应当前采集")
        if value.get("raw_sha256") != store.capture(key)[2]["raw_sha256"]:
            raise ValueError("标注引用的原始数据校验值不一致")
        st.session_state[imported_key] = value
        st.session_state[f"{editor_key}_generation"] = st.session_state.get(f"{editor_key}_generation", 0) + 1
        st.rerun()
    base = st.session_state.get(imported_key, old) or {"events": [], "intervals": [], "notes": ""}
    window = time_window(frame, f"annotation_window_{key}")
    if window:
        raw_plot(frame, *window, annotation=base)
        st.caption("绿色竖线为人工动作，绿色背景为有效区间，红色背景为排除区间。")
    st.caption("动作时间使用设备相对秒数，必须填写 segment。只有 valid 区间参与评估；exclude 区间优先排除。")
    generation = st.session_state.get(f"{editor_key}_generation", 0)
    with st.form(f"{editor_key}_form_{generation}"):
        events = st.data_editor(pd.DataFrame(base["events"], columns=EVENT_COLUMNS), num_rows="dynamic",
                                hide_index=True, key=f"{editor_key}_events_{generation}")
        intervals = st.data_editor(pd.DataFrame(base["intervals"], columns=INTERVAL_COLUMNS), num_rows="dynamic",
            hide_index=True, key=f"{editor_key}_intervals_{generation}",
            column_config={"kind": st.column_config.SelectboxColumn("kind", options=["valid", "exclude"], required=True)})
        notes = st.text_area("标注备注", base.get("notes", ""))
        saved = st.form_submit_button("保存为新标注版本")
    if saved:
        revision = store.save_annotation(key, events.to_dict("records"), intervals.to_dict("records"), notes, revision_id)
        notice(f"已保存标注 {revision['id']}；原版本保持不变。")
        st.rerun()
    if old:
        st.download_button("导出当前标注", store.object_path("annotation", revision_id).read_bytes(), f"{revision_id}.json")
        if window:
            times = [e["time"] for e in old["events"] if e["segment"] == window[0] and window[1] <= e["time"] <= window[2]]
            st.caption(f"当前窗口人工动作时间：{times}")


def experiment_page(store):
    datasets = store.list_objects("dataset")
    if not datasets:
        st.info("请先在数据页建立数据集，或创建合成流程示例。")
        return
    job = st.session_state.get("infra_job")
    active = job is not None and job.thread is not None and job.thread.is_alive()
    runs = store.list_objects("run")
    template = st.selectbox("从旧实验加载配置", [None] + [r["id"] for r in runs], format_func=lambda v: v or "默认配置")
    if st.session_state.get("config_template") != template:
        st.session_state.config_template = template
        config = read_json(store.object_path("run", template) / "config.json") if template else ExperimentConfig().to_dict()
        for name, value in config["algorithm"].items():
            st.session_state[f"cfg_{name}"] = value
        st.session_state.cfg_tolerance = config["tolerance_seconds"]
    with st.form("experiment_config"):
        dataset_id = st.selectbox("数据集快照", [d["id"] for d in datasets],
                                  format_func=lambda v: next(d["name"] for d in datasets if d["id"] == v) + " · " + v)
        signal = st.selectbox("分析信号", ["Magnitude", "X", "Y", "Z"], key="cfg_signal")
        cutoff = st.number_input("截止频率 / Hz", min_value=0.01, value=5.0, key="cfg_cutoff_hz")
        prominence = st.number_input("峰显著度 / m/s²", min_value=0.001, value=0.7, key="cfg_prominence")
        distance = st.number_input("最小峰间距 / s", min_value=0.001, value=0.4, key="cfg_min_peak_interval")
        gap = st.number_input("连续段缺口阈值 / s", min_value=0.001, value=0.2, key="cfg_max_gap")
        tolerance = st.number_input("事件匹配容差 / s", min_value=0.001, value=0.1, key="cfg_tolerance")
        submitted = st.form_submit_button("运行实验", disabled=active)
    if submitted:
        config = ExperimentConfig(AlgorithmConfig(signal, cutoff, prominence, distance, gap), tolerance)
        st.session_state.infra_job = ExperimentJob(store, dataset_id, config).start()
        st.rerun()
    if job:
        if st.button("取消当前实验", disabled=not active):
            job.cancel()
            st.info("已请求取消，将在当前片段完成后停止，保留已有结果。")

        @st.fragment(run_every=0.5 if active else None)
        def progress():
            snapshot = job.snapshot()
            st.progress(snapshot["completed"] / max(1, snapshot["total"]))
            st.json(snapshot)
            if snapshot["error"]:
                st.error(snapshot["error"])
            st.caption("取消在片段边界生效；实验执行不依赖页面刷新。")
        progress()
        st.button("刷新任务状态")
    st.subheader("实验历史")
    st.dataframe(pd.DataFrame([{"id": r["id"], "数据集": r["dataset_id"], "状态": r["status"],
                               "已完成": r["completed"], "错误": r["error"]} for r in runs]), hide_index=True)


def comparison_signal(store, directory, run_id, analysis, cases):
    selection = st.selectbox("查看片段", [int(i) for i in analysis.selection.unique()], key=f"selection_{run_id}")
    item = read_json(directory / "dataset.json")["items"][selection]
    frame = None
    try:
        source, _, hashes = store.capture(item["capture_key"])
        if any(hashes[k] != item[k] for k in hashes):
            st.warning("原始采集已变化，仅展示保存的实验产物。")
        else:
            frame = source[source.segment == item["segment"]]
    except (OSError, ValueError) as exc:
        st.warning(f"原始采集不可用，仍可查看和导出实验产物：{exc}")
    failures = cases[(cases.selection == selection) & (cases.kind != "matched")]
    st.dataframe(failures, hide_index=True)
    failed = st.selectbox("定位失败事件", [None] + list(failures.index),
        format_func=lambda v: "手动选择时间窗" if v is None else f"{failures.loc[v, 'kind']} @ {failures.loc[v, 'time']:.3f}s",
        key=f"failure_{run_id}_{selection}")
    focus = None
    marker = (selection, failed)
    if failed is not None and st.session_state.get(f"focus_{run_id}") != marker:
        focus = (item["segment"], float(failures.loc[failed, "time"]))
    st.session_state[f"focus_{run_id}"] = marker
    saved = analysis[analysis.selection == selection]
    timeline = frame if frame is not None else saved.assign(device="saved-experiment")
    window = time_window(timeline, f"compare_{run_id}_{selection}", focus)
    if not window:
        return
    if frame is not None:
        raw_plot(frame, *window)
    part = saved[saved.time.between(window[1], window[2])]
    fig, ax = plt.subplots(figsize=(11, 3))
    for _, group in part.groupby("span"):
        ax.plot(group.time, group.raw_acceleration, color="lightblue")
        ax.plot(group.time, group.filtered_acceleration, color="royalblue")
    candidates = part[part.is_peak == 1]
    ax.scatter(candidates.time, candidates.filtered_acceleration, color="red", label="Candidate peaks")
    annotation_path = directory / "inputs" / str(selection) / "annotation.json"
    if annotation_path.exists():
        annotation = read_json(annotation_path)
        for e in annotation["events"]:
            if e["segment"] == item["segment"] and window[1] <= e["time"] <= window[2]:
                ax.axvline(e["time"], color="green", alpha=0.5)
        for interval in annotation["intervals"]:
            if interval["segment"] == item["segment"] and interval["end"] >= window[1] and interval["start"] <= window[2]:
                ax.axvspan(max(window[1], interval["start"]), min(window[2], interval["end"]),
                           color="green" if interval["kind"] == "valid" else "red", alpha=0.1)
    ax.set(xlabel="Device-relative time (s)", ylabel="m/s²", xlim=(window[1], max(window[2], window[1] + 0.01)))
    ax.legend()
    fig.tight_layout()
    st.pyplot(fig)
    plt.close(fig)
    st.caption("浅蓝：重采样原始信号；蓝：滤波；红点：候选波峰；绿线：人工动作。")


def comparison_page(store):
    runs = [r for r in store.list_objects("run") if r["status"] not in ("queued", "running")]
    if not runs:
        st.info("暂无已结束实验。请先到实验页运行。")
        return
    selected = st.multiselect("选择对比实验", [r["id"] for r in runs], default=[runs[0]["id"]])
    rows = []
    for run_id in selected:
        directory = store.object_path("run", run_id)
        if not (directory / "metrics.json").exists():
            st.warning(f"{run_id} 未留下完整产物，请查看实验错误。")
            continue
        manifest = read_json(directory / "manifest.json")
        config = read_json(directory / "config.json")
        metrics = read_json(directory / "metrics.json")
        rows.append({"实验": run_id, "数据集": manifest["dataset_id"], "状态": manifest["status"],
                     **config["algorithm"], "tolerance_seconds": config["tolerance_seconds"], **metrics["overall"]})
        with st.expander(run_id, expanded=len(selected) == 1):
            st.json(metrics)
            st.caption("not_evaluated＝未评估；partial＝仅部分片段已评估。候选波峰不等于已验证动作。")
            analysis = pd.read_csv(directory / "analysis.csv")
            cases = pd.read_csv(directory / "cases.csv")
            if not analysis.empty:
                comparison_signal(store, directory, run_id, analysis, cases)
            for name in ("analysis.csv", "events.csv", "cases.csv", "metrics.json", "config.json", "report.html"):
                path = directory / name
                if path.exists():
                    st.download_button(f"下载 {name}", path.read_bytes(), f"{run_id}_{name}", key=f"download_{run_id}_{name}")
            st.download_button("导出实验包", export_run(store, run_id), f"{run_id}.zip", key=f"zip_{run_id}")
            st.caption("实验包包含配置、代码差异、标注快照和结果；原始采集需另行导出和备份。")
    if rows:
        st.subheader("参数与指标对比")
        st.dataframe(pd.DataFrame(rows), hide_index=True)
        if len({r["数据集"] for r in rows}) > 1:
            st.warning("所选实验使用不同数据集，不宜直接比较汇总指标。")


def main():
    st.set_page_config(page_title="BeatDance Infra", page_icon="🔬", layout="wide")
    st.title("动随节拍 · 开发工作台")
    st.caption("本地采集、版本化标注、离线实验与评估。候选波峰与人工真值分开管理。")
    store = Store(Path(os.environ.get("BEATDANCE_INFRA_ROOT", str(PROJECT_ROOT))))
    page = st.sidebar.radio("工作台", PAGES, key="infra_page")
    receiver = st.session_state.get("infra_receiver")
    if page != "采集" and receiver is not None and receiver.snapshot(read_rows=False)[1]["running"]:
        stop_capture(receiver)
    if "infra_notice" in st.session_state:
        st.info(st.session_state.pop("infra_notice"))
    try:
        {"采集": capture_page, "数据": data_page, "标注": annotation_page,
         "实验": experiment_page, "对比": comparison_page}[page](store)
    except (OSError, ValueError, KeyError, TypeError, BusyError) as exc:
        st.error(f"操作未完成：{exc}")
