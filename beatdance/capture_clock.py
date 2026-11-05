"""Shared filming reference for the two live capture pages."""

import streamlit as st


def render_capture_clock(rows, state, capture_id: str) -> None:
    """Show the latest persisted sample time, without extrapolating during gaps."""
    st.caption(f"视频对时 · 批次 {capture_id}")
    if not rows:
        st.markdown("# 采集时间：等待数据")
        st.caption("收到第一条有效样本后从 0 秒开始；请在收到数据后拍摄屏幕。")
        return

    sample = rows[-1]
    segment = int(sample.get("seg", sample.get("segment", 0)))
    st.markdown(f"# 采集时间 {float(sample['time']):.3f} s")
    st.caption(f"片段 {segment} · 样本序号 {int(sample['seq'])} · 对应 CSV 的 time 列；设备重启后新片段重新计时。")
    if not state["running"]:
        st.info("采集已停止：时间停留在最后保存的样本。")
    elif state["age"] is None or state["age"] >= 2:
        st.warning("数据已中断：显示的是最后保存的样本时间，请等待恢复后再对时。")
    st.caption("录制时先拍清批次、片段和时间，再连续拍动作，结束前再拍一次屏幕。"
               "页面约每 0.5 秒刷新，另有网络和渲染延迟；小数位不代表同步精度，逐次计数仍需用明显动作校准。")
