"""CSV loading and reproducible derived datasets; raw captures stay untouched."""

import json
from pathlib import Path

import numpy as np
import pandas as pd

from .imu_udp import utc_now
from .counting import ALGORITHM_ID, ANALYSIS_COLUMNS, AXES, algorithm_metadata, detect
from .running_rhythm import ALGORITHM_ID as RUNNING_ALGORITHM_ID, MAX_GAP_SECONDS, RunningRhythmCounter
from .signal_processing import axis_trace


SIGNALS = ("Magnitude", "X", "Y", "Z")
RUNNING_EVENTS_FILE = "running_events.csv"
RUNNING_EVENT_COLUMNS = ("segment", "type", "event_s", "published_s",
                         "steps_per_min", "swings_per_min", "is_cycle_end")

# 硬件报文里的扁平字段 -> 分析/显示层使用的规范字段。
FLAT_TO_CANONICAL = {
    "seg": "segment",
    "ax": "ax_g", "ay": "ay_g", "az": "az_g",
    "gx": "gx_dps", "gy": "gy_dps", "gz": "gz_dps",
}
CANONICAL_ACCELERATION = ("ax_g", "ay_g", "az_g")
CANONICAL_GYRO = ("gx_dps", "gy_dps", "gz_dps")
TEXT_COLUMNS = ("device", "sender_ip", "imu_id")
UINT32 = 2**32


def normalize_frame(frame):
    """把任一种已保存的原始 CSV schema 统一成分析层期望的列名。

    兼容两种历史格式：早期 `pc_time/ax_g/segment/time` 17 列格式，
    以及当前固件的 `host_s/seg/ax/t_us` 扁平格式。缺少 `time` 列时按
    每段内的设备时间戳累加还原，含 uint32 回绕处理。空采集（只有表头）
    也必须补齐列名，否则停止生成分析文件会报 KeyError。
    """
    if not isinstance(frame, pd.DataFrame):
        frame = pd.DataFrame(frame)
    if frame.columns.empty:
        return frame

    frame = frame.copy()
    # 只追加规范列，不改名：infra 工作台的实时图直接读 seg/t_us/ax，
    # 监测应用与分析层读 segment/ax_g，两套调用方共用同一份数据。
    for source, target in FLAT_TO_CANONICAL.items():
        if source in frame.columns and target not in frame.columns:
            frame[target] = frame[source]

    if "segment" not in frame.columns:
        frame["segment"] = 0

    for key in frame.columns.difference(list(TEXT_COLUMNS)):
        frame[key] = pd.to_numeric(frame[key], errors="raise")

    if "time" not in frame.columns:
        if "t_us" in frame.columns:
            delta = frame.groupby("segment", sort=False)["t_us"].diff().fillna(0.0) % UINT32
            frame["time"] = delta.groupby(frame["segment"], sort=False).cumsum() / 1e6
        else:
            frame["time"] = 0.0

    for key in (*CANONICAL_ACCELERATION, *CANONICAL_GYRO):
        if key not in frame.columns:
            raise ValueError(f"raw CSV 缺少 {key} 列")

    return frame.sort_values(["segment", "time"], kind="stable").reset_index(drop=True)


def rows_to_frame(rows):
    return normalize_frame(pd.DataFrame(rows))


def load_raw_csv(path):
    return normalize_frame(pd.read_csv(path))


def capture_mode(metadata):
    """Older running captures only carry an activity label."""
    mode = metadata.get("capture_mode")
    if mode is None:
        activity = metadata.get("labels", {}).get("activity")
        if isinstance(activity, str):
            mode = activity.strip().lower()
    return "running" if mode == "running" else "jump-rope"


def running_events(raw):
    counter = RunningRhythmCounter()
    events = []
    for segment, time, *axes in raw[["segment", "time", *AXES]].itertuples(index=False, name=None):
        update = counter.push(time, *axes, segment=segment)
        rates = (update.steps_per_min, update.swings_per_min)
        events.extend((segment, "step", event, time, *rates, 0) for event in update.steps)
        cycle_start = len(update.arm_turns) - update.arm_cycles
        events.extend((segment, "arm_turn", event, time, *rates, int(index >= cycle_start))
                      for index, event in enumerate(update.arm_turns))
    return pd.DataFrame(events, columns=RUNNING_EVENT_COLUMNS), {
        "step_count": counter.step_count, "arm_cycle_count": counter.arm_cycle_count,
    }


def save_analysis(directory, signal="Magnitude"):
    """Analyze the entire saved capture (each boot and continuous span separately)."""
    if signal not in SIGNALS:
        raise ValueError("unknown acceleration signal")
    directory = Path(directory)
    raw = load_raw_csv(directory / "imu_raw.csv")
    metadata_path = directory / "session.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if signal == "Magnitude" and capture_mode(metadata) == "running":
        analysis, summary = running_events(raw)
        path = directory / RUNNING_EVENTS_FILE
        details = {"algorithm_id": RUNNING_ALGORITHM_ID, "max_gap_seconds": MAX_GAP_SECONDS, **summary}
    else:
        if signal == "Magnitude":
            analysis = detect(raw).analysis
        else:
            traces = [axis_trace(frame, signal) for _, frame in raw.groupby("segment", sort=False)]
            analysis = (pd.concat(traces, ignore_index=True) if traces else
                        pd.DataFrame(columns=ANALYSIS_COLUMNS[:6]))
        analysis.insert(0, "signal", signal)
        path = directory / f"analysis_{signal.lower()}.csv"
        details = {
            "signal": signal, "acceleration_unit": "m/s^2",
            "algorithm_id": ALGORITHM_ID if signal == "Magnitude" else None,
            "filter": "5 Hz causal low-pass", "max_gap_seconds": algorithm_metadata()["max_gap"],
        }
    temp = path.with_suffix(".csv.tmp")
    analysis.to_csv(temp, index=False)
    temp.replace(path)
    metadata.setdefault("derived_files", {})[path.name] = {
        "created_at_utc": utc_now(),
        "input": "imu_raw.csv (all accepted samples)", "rows": len(analysis),
        **details,
    }
    temp_metadata = metadata_path.with_suffix(".json.tmp")
    temp_metadata.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp_metadata.replace(metadata_path)
    return path
