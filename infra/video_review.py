"""Offline, browser-local video review; video playback owns the shared cursor."""

import json
from pathlib import Path

import streamlit as st

from beatdance.counting import ALGORITHM_ID, algorithm_metadata, detect


def build_review_html(frame, capture_key: str, raw_sha256: str, segment: int) -> str:
    """Embed full-resolution span-separated analysis, never the video bytes."""
    part = frame.loc[frame.segment == segment]
    if part.empty:
        raise ValueError("所选启动片段没有样本")
    analysis = detect(part).analysis
    spans = []
    for _, group in analysis.groupby("span", sort=False):
        spans.append(group[["time", "raw_acceleration", "filtered_acceleration", "is_peak"]].values.tolist())
    payload = {"schema_version": 1, "capture_key": capture_key, "raw_sha256": raw_sha256,
               "segment": int(segment), "algorithm": algorithm_metadata(), "algorithm_id": ALGORITHM_ID, "spans": spans,
               "low": float(part.time.min()), "high": float(part.time.max())}
    # JSON lives inside a script element: escape markup even in capture names.
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).replace("<", "\\u003c")
    return Path(__file__).with_name("video_review.html").read_text(encoding="utf-8").replace("__REVIEW_DATA__", encoded)



def build_comparison_review_html(runs, selection: int, focus=None) -> str:
    """Use saved experiment traces/labels, without re-running any detector."""
    from .store import read_json
    from .counting_lab import paired_cases
    import math
    if not runs:
        raise ValueError("请选择实验")
    item = read_json(runs[0]["directory"] / "dataset.json")["items"][selection]
    tracks, cases = [], []
    for run in runs:
        analysis = run["analysis"]
        part = analysis[analysis.selection == selection]
        spans = [g[["time", "raw_acceleration", "filtered_acceleration", "is_peak"]].values.tolist()
                 for _, g in part.groupby("span", sort=False)]
        if not spans:
            raise ValueError("实验片段没有样本")
        errors = run["cases"]
        errors = errors[(errors.selection == selection) & (errors.kind != "matched")]
        entries = [dict(time=float(row.time), kind=row.kind, label=run["id"] + " · " + row.kind)
                   for row in errors.itertuples()]
        cases.extend(entries)
        tracks.append(dict(id=run["id"], label=run["config"]["algorithm_id"], config=run["config"],
                           spans=spans, errors=entries))
    truth, intervals = [], []
    path = runs[0]["directory"] / "inputs" / str(selection) / "annotation.json"
    if path.exists():
        annotation = read_json(path)
        truth = [e["time"] for e in annotation["events"] if e["segment"] == item["segment"] and item["start"] <= e["time"] <= item["end"]]
        intervals = [r for r in annotation["intervals"] if r["segment"] == item["segment"]]
    if len(runs) >= 2:
        paired = paired_cases(runs[0]["cases"], runs[1]["cases"])
        for row in paired[(paired.selection == selection) & (paired.change != "both matched")].itertuples():
            cases.append(dict(time=float(row.truth_time), kind=row.change, label="A/B · " + row.change))
    context = dict(selection=int(selection), dataset_sha256=runs[0]["manifest"]["dataset_sha256"],
                   runs=[dict(id=r["id"], config=r["config"]) for r in runs])
    payload = dict(schema_version=1, capture_key=item["capture_key"], raw_sha256=item["raw_sha256"],
                   segment=int(item["segment"]), algorithm=runs[0]["config"]["algorithm"],
                   spans=tracks[0]["spans"], tracks=tracks, truth=truth, intervals=intervals,
                   cases=sorted(cases, key=lambda r:r["time"]), review_context=context,
                   low=float(item["start"]), high=float(item["end"]),
                   focus=float(focus) if focus is not None and math.isfinite(focus) else None)
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).replace("<", "\\u003c")
    return Path(__file__).with_name("video_review.html").read_text(encoding="utf-8").replace("__REVIEW_DATA__", encoded)


def comparison_video_panel(runs, selection, focus=None):
    html = build_comparison_review_html(runs, selection, focus)
    st.caption("视频、所有算法轨道与差异定位共用时间轴。先选本地视频并填写同步点；播放器内操作不会重载。更换实验／片段或上方参数会重载，请先导出对齐记录。")
    st.download_button("下载视频与算法对比页", html, "counting-video-review.html", mime="text/html")
    st.iframe(html, height=max(1450, 850 + 170 * len(runs)))


def comparison_video_page(store):
    from .counting_lab import load_comparison
    runs = [r for r in store.list_objects("run") if r["status"] == "completed"]
    selected = st.multiselect("视频对比实验（相同数据集）", [r["id"] for r in runs])
    if not selected:
        st.info("选择已完成实验，或先到算法实验台批量运行。")
        return
    compared = load_comparison(store, selected)
    selections = [int(i) for i in compared[0]["analysis"].selection.unique()]
    if selections:
        selection = st.selectbox("视频对比片段", selections)
        comparison_video_panel(compared, selection)


def video_review_page(store) -> None:
    from .workbench import capture_selector

    st.subheader("视频复核")
    mode = st.radio("复核内容", ["Loss Longer 计数复核", "算法实验对比"], horizontal=True)
    if mode == "算法实验对比":
        comparison_video_page(store)
        return
    st.caption("选择采集与启动片段，再在播放器中选择本地视频。视频仅在浏览器读取，不上传或复制到采集目录。")
    key = capture_selector(store, "video_review_capture")
    if key is None:
        return
    frame, _, hashes = store.capture(key)
    if frame.empty:
        st.info("该记录没有样本。")
        return
    segment = st.selectbox("复核启动片段", [int(s) for s in frame.segment.unique()])
    html = build_review_html(frame, key, hashes["raw_sha256"], segment)
    st.caption("修改上方采集或片段会重载播放器；请先导出对齐与复核记录。下载独立复核页后也可离线使用。")
    st.download_button("下载独立复核页 HTML", html, "video-review.html", mime="text/html")
    st.iframe(html, height=1100)
