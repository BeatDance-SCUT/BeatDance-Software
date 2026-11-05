"""Offline, browser-local video review; video playback owns the shared cursor."""

import json
from pathlib import Path

import streamlit as st

from beatdance.signal_processing import AlgorithmConfig, analyze_imu


def build_review_html(frame, capture_key: str, raw_sha256: str, segment: int,
                      config: AlgorithmConfig) -> str:
    """Embed full-resolution span-separated analysis, never the video bytes."""
    part = frame.loc[frame.segment == segment]
    if part.empty:
        raise ValueError("所选启动片段没有样本")
    analysis = analyze_imu(part, config=config)
    spans = []
    for _, group in analysis.groupby("span", sort=False):
        spans.append(group[["time", "raw_acceleration", "filtered_acceleration", "is_peak"]].values.tolist())
    payload = {"schema_version": 1, "capture_key": capture_key, "raw_sha256": raw_sha256,
               "segment": int(segment), "algorithm": config.to_dict(), "spans": spans,
               "low": float(part.time.min()), "high": float(part.time.max())}
    # JSON lives inside a script element: escape markup even in capture names.
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).replace("<", "\\u003c")
    return Path(__file__).with_name("video_review.html").read_text(encoding="utf-8").replace("__REVIEW_DATA__", encoded)


def video_review_page(store) -> None:
    from .workbench import capture_selector

    st.subheader("视频复核")
    st.caption("选择采集与启动片段，再在播放器中选择本地视频。视频仅在浏览器读取，不上传或复制到采集目录。")
    key = capture_selector(store, "video_review_capture")
    if key is None:
        return
    frame, _, hashes = store.capture(key)
    if frame.empty:
        st.info("该记录没有样本。")
        return
    segment = st.selectbox("复核启动片段", [int(s) for s in frame.segment.unique()])
    with st.expander("波峰分析参数"):
        defaults = AlgorithmConfig()
        signal = st.selectbox("复核信号", ["Magnitude", "X", "Y", "Z"])
        prominence = st.number_input("复核峰显著度 / m/s²", min_value=0.001, value=defaults.prominence)
        interval = st.number_input("复核最小峰间距 / s", min_value=0.001, value=defaults.min_peak_interval)
    config = AlgorithmConfig(signal=signal, prominence=prominence, min_peak_interval=interval)
    html = build_review_html(frame, key, hashes["raw_sha256"], segment, config)
    st.caption("修改上方采集、片段或参数会重载播放器；请先导出对齐与复核记录。下载独立复核页后也可离线使用。")
    st.download_button("下载独立复核页 HTML", html, "video-review.html", mime="text/html")
    st.iframe(html, height=1100)
