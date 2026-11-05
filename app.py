import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import streamlit as st

from beatdance.imu_udp import UDPReceiver
from beatdance.capture_clock import render_capture_clock
from beatdance.paths import DEFAULT_RECORDINGS, EXAMPLE_ANALYSIS
from beatdance.recordings import SIGNALS, load_raw_csv, rows_to_frame, save_analysis
from beatdance.signal_processing import analyze_imu, peak_intervals


st.set_page_config(page_title="Jump Rope Monitor", page_icon="🏃", layout="wide")
st.title("IMU 跳绳运动监测")
st.caption("BeatDance · JSON → CSV → 可视化")


def render_analysis(df):
    required = ["time", "raw_acceleration", "filtered_acceleration", "is_peak"]
    if not all(key in df.columns for key in required):
        st.error("分析 CSV 必须包含 time、raw_acceleration、filtered_acceleration、is_peak。")
        return
    try:
        df = df.copy()
        df[required] = df[required].apply(pd.to_numeric, errors="raise")
        if df.empty or not np.isfinite(df[required].to_numpy()).all() or not df["is_peak"].isin([0, 1]).all():
            raise ValueError("empty or invalid data")
    except (TypeError, ValueError):
        st.error("分析 CSV 为空或含有无效数值。")
        return
    groups = [key for key in ("signal", "segment", "span") if key in df.columns]
    df = df.sort_values([*groups, "time"]).reset_index(drop=True)
    peaks = df[df["is_peak"] == 1]
    intervals = peak_intervals(df)
    series = df.groupby(groups, sort=False) if groups else [(0, df)]
    duration = 0.0
    for _, frame in series:
        duration += float(frame["time"].max() - frame["time"].min())
    average = float(intervals["delta_t"].mean()) if not intervals.empty else None
    cards = st.columns(5)
    cards[0].metric("检测波峰", len(peaks))
    cards[1].metric("估计波峰频率 / 分钟", f"{60 / average:.1f}" if average else "N/A")
    cards[2].metric("平均波峰间隔 / 秒", f"{average:.3f}" if average else "N/A")
    cards[3].metric("有效片段时长 / 秒", f"{duration:.1f}")
    cards[4].metric("分析采样点", len(df))
    fig, ax = plt.subplots(figsize=(12, 4))
    series = df.groupby(groups, sort=False) if groups else [(0, df)]
    for index, (_, frame) in enumerate(series):
        ax.plot(frame["time"], frame["raw_acceleration"], color="lightskyblue", alpha=0.8,
                label="Raw" if index == 0 else None)
        ax.plot(frame["time"], frame["filtered_acceleration"], color="royalblue",
                label="Filtered" if index == 0 else None)
    ax.scatter(peaks["time"], peaks["filtered_acceleration"], color="red", s=35, label="Peaks", zorder=5)
    ax.set(xlabel="Device-relative time (s)", ylabel="Acceleration (m/s²)")
    ax.grid(True, alpha=0.3)
    ax.legend()
    fig.tight_layout()
    st.pyplot(fig)
    plt.close(fig)
    st.subheader("波峰间隔随时间变化 · Δt–t")
    st.caption("横轴 t 为后一个波峰的设备相对时间；纵轴 Δt = tᵢ − tᵢ₋₁，单位均为秒。"
               "每段首个波峰不产生间隔，不跨设备重启、数据缺口或不同信号计算和连线。")
    if intervals.empty:
        st.info("当前视图尚无有效间隔：同一连续片段内至少需要两个不同时间的波峰。")
    else:
        fig, ax = plt.subplots(figsize=(12, 3))
        series = intervals.groupby(groups, sort=False) if groups else [(0, intervals)]
        for index, (_, frame) in enumerate(series):
            ax.plot(frame["time"], frame["delta_t"], color="darkorange", marker="o",
                    markersize=4, label="Peak interval" if index == 0 else None)
        ax.axhline(average, color="gray", linestyle="--", label=f"Mean: {average:.3f} s")
        ax.set(xlabel="Device-relative time (s)", ylabel=r"$\Delta t$ (s)", ylim=(0, None))
        ax.grid(True, alpha=0.3)
        ax.legend()
        fig.tight_layout()
        st.pyplot(fig)
        plt.close(fig)
        with st.expander("间隔数据（当前视图）"):
            st.dataframe(intervals, hide_index=True)
            st.download_button("下载当前视图间隔 CSV", intervals.to_csv(index=False).encode("utf-8"),
                               "peak_intervals.csv", "text/csv")
    with st.expander("波峰位置"):
        st.dataframe(peaks[[*groups, "time", "filtered_acceleration"]], hide_index=True)
    st.caption("波峰数量不是已验证的跳绳次数。实时窗口采用零相位离线滤波，窗口边缘的结果可能随新数据变化。")


def render_imu(raw, window, signal):
    latest = raw[raw["segment"] == raw["segment"].iloc[-1]]
    latest = latest[latest["time"] >= latest["time"].iloc[-1] - window]
    st.subheader("六轴原始数据（从已保存的 CSV 读取）")
    left, right = st.columns(2)
    left.caption("加速度 / g（含重力）")
    left.line_chart(latest.set_index("time")[["ax_g", "ay_g", "az_g"]])
    right.caption("角速度 / °/s")
    right.line_chart(latest.set_index("time")[["gx_dps", "gy_dps", "gz_dps"]])
    # 扁平协议没有 device/temp_c 等字段，历史格式才有；缺列时跳过而不是报错。
    facts = []
    if "device" in latest.columns:
        facts.append(f"设备：{latest['device'].iloc[-1]}")
    if "sender_ip" in latest.columns:
        facts.append(f"来源：{latest['sender_ip'].iloc[-1]}")
    if "temp_c" in latest.columns:
        facts.append(f"温度：{latest['temp_c'].iloc[-1]:.2f} °C")
    facts.append(f"启动片段：{int(latest['segment'].iloc[-1])}")
    st.caption(" · ".join(facts))
    st.subheader(f"加速度分析 · {signal} · 最近 {window} 秒")
    analysis = analyze_imu(latest, signal)
    render_analysis(analysis)
    return analysis


def finalize(receiver):
    receiver.stop()
    try:
        path = save_analysis(receiver.directory)
        st.session_state.capture_notice = ("success", f"采集已保存，完整合加速度分析：{path}")
    except (OSError, ValueError, KeyError) as exc:
        st.session_state.capture_notice = ("error", f"原始 CSV 仍保留在 {receiver.directory}；生成分析文件失败：{exc}")


mode = st.sidebar.radio("数据来源", ["分析 CSV", "UDP 实时采集", "历史采集"])
receiver = st.session_state.get("udp_receiver")
if mode != "UDP 实时采集" and receiver is not None:
    if receiver.snapshot(read_rows=False)[1]["running"]:
        finalize(receiver)

if "capture_notice" in st.session_state:
    level, message = st.session_state.capture_notice
    getattr(st, level)(message)

if mode == "分析 CSV":
    st.info("当前为离线分析模式。可切换到 UDP 实时采集接入硬件。默认 CSV 来自模拟数据。")
    uploaded = st.sidebar.file_uploader("上传分析 CSV", type=["csv"])
    source = uploaded if uploaded is not None else EXAMPLE_ANALYSIS
    try:
        df = pd.read_csv(source)
    except (OSError, ValueError) as exc:
        st.error(f"无法读取 CSV：{exc}")
        st.stop()
    render_analysis(df)
    st.download_button("下载分析 CSV", df.to_csv(index=False).encode("utf-8"),
                       "analysis_result.csv", "text/csv")

elif mode == "UDP 实时采集":
    st.info("开始后自动逐行保存原始 CSV，再读取 CSV 更新曲线。首次收到的设备与发送 IP 会锁定本次采集。")
    running = receiver is not None and receiver.snapshot(read_rows=False)[1]["running"]
    # 采集期间控件被禁用，重新渲染会回落到默认值；记住上次实际使用的地址与端口。
    saved_bind, saved_port = st.session_state.get("capture_endpoint", ("0.0.0.0", 5005))
    bind = st.sidebar.text_input("本地监听地址", saved_bind, disabled=running)
    port = int(st.sidebar.number_input("UDP 端口", min_value=1, max_value=65535,
                                       value=int(saved_port), disabled=running))
    st.sidebar.caption(f"自动存储位置：{DEFAULT_RECORDINGS}")
    participant = st.sidebar.text_input("参与者编号（可选）", disabled=running)
    activity = st.sidebar.text_input("动作标签（可选，例如 jump_rope）", disabled=running)
    notes = st.sidebar.text_area("采集备注（可选，例如佩戴位置）", disabled=running)
    window = st.sidebar.slider("显示窗口 / 秒", 5, 120, 10)
    signal = st.sidebar.selectbox("分析加速度", SIGNALS)
    start_col, stop_col = st.sidebar.columns(2)
    if start_col.button("开始新采集", disabled=running):
        if receiver is not None:
            receiver.stop()
        new_receiver = None
        try:
            new_receiver = UDPReceiver(bind, port, output_dir=DEFAULT_RECORDINGS)
            new_receiver.set_labels(participant, activity, notes)
            st.session_state.udp_receiver = new_receiver.start()
            st.session_state.pop("capture_notice", None)
        except (OSError, ValueError) as exc:
            if new_receiver is not None:
                new_receiver.stop()
            st.error(f"无法开始采集：{exc}。检查监听地址、目录权限及端口是否被其他程序占用。"
                     "同一时间只能有一个入口监听该端口，请先关掉另一个正在运行的 app 或 infra_app。")
        else:
            st.session_state.capture_endpoint = (bind, port)
            st.rerun()
    if stop_col.button("停止并保存", disabled=not running):
        finalize(receiver)
        st.rerun()

    @st.fragment(run_every=0.5 if running else None)
    def live_view():
        current = st.session_state.get("udp_receiver")
        if current is None:
            st.warning("尚未开始采集。将硬件 PC_IP 设为本机的局域网 IPv4，PC_PORT 与此处端口保持一致。")
            return
        rows, state = current.snapshot()
        render_capture_clock(rows, state, current.capture_id)
        st.code(str(current.directory), language=None)
        if state["error"]:
            st.error(state["error"])
        if not state["running"]:
            st.info("采集已停止。CSV 已保留；新采集会使用新的目录。")
            if running:
                st.rerun()
        elif state["age"] is None or state["age"] >= 2:
            st.warning("等待 UDP 数据：请检查同一 Wi-Fi、硬件 PC_IP、端口和 Windows 防火墙。")
        else:
            st.success("正在接收并写入 CSV")
        cols = st.columns(4)
        cols[0].metric("已写入 CSV", state["packets"])
        cols[1].metric("最近 1 秒样本", state["packet_rate"])
        cols[2].metric("序号缺口", state["gaps"])
        cols[3].metric("无效报文", state["invalid"])
        st.caption(f"重复/乱序丢弃：{state['out_of_order']} · 其他设备忽略：{state['ignored_device']} · "
                   f"检测重启：{state['restarts']}。序号缺口不等同于纯网络丢包。")
        if rows:
            render_imu(rows_to_frame(rows), window, signal)
        st.caption("显示只读取最后 30,000 行；磁盘原始 CSV 持续追加并保留全部已接受样本。停止后可在历史采集中下载。")

    live_view()

else:
    manifests = sorted(DEFAULT_RECORDINGS.glob("*/*/session.json"), reverse=True)
    if not manifests:
        st.info("还没有历史采集。先在 UDP 实时采集中开始一次采集。")
        st.stop()
    selected = st.sidebar.selectbox("采集批次（UTC）", manifests,
                                    format_func=lambda path: f"{path.parent.parent.name} / {path.parent.name}")
    directory = selected.parent
    st.code(str(directory), language=None)
    st.json(selected.read_text(encoding="utf-8"), expanded=False)
    try:
        raw = load_raw_csv(directory / "imu_raw.csv")
    except (OSError, ValueError, KeyError) as exc:
        st.error(f"无法读取原始 CSV：{exc}")
        st.stop()
    if not raw.empty:
        segment = st.sidebar.selectbox("设备启动片段", raw["segment"].unique())
        window = st.sidebar.slider("显示窗口 / 秒", 5, 120, 10)
        signal = st.sidebar.selectbox("分析加速度", SIGNALS)
        render_imu(raw[raw["segment"] == segment], window, signal)
        if st.button("生成完整采集的分析 CSV（所有启动片段）"):
            try:
                st.success(f"已保存：{save_analysis(directory, signal)}")
            except (OSError, ValueError, KeyError) as exc:
                st.error(f"生成分析文件失败：{exc}")
    for path in [directory / "imu_raw.csv", selected, *sorted(directory.glob("analysis_*.csv"))]:
        st.download_button(f"下载 {path.name}", path.read_bytes(), path.name,
                           "application/json" if path.suffix == ".json" else "text/csv",
                           key=str(path))
