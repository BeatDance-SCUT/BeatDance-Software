"""Shared acceleration analysis for simulated and received IMU data."""

import numpy as np
import pandas as pd
from scipy.signal import butter, find_peaks, sosfiltfilt


STANDARD_GRAVITY = 9.80665


def analyze_acceleration(times, acceleration, *, max_gap=0.2):
    """Resample each continuous span on device time, then filter and find peaks.

    UDP gaps longer than max_gap seconds are not interpolated or filtered across.
    Short spans (<1 second) are shown raw and are not counted as peaks. Results
    are a retrospective window estimate; edge peaks can change as data arrives.
    """
    times = np.asarray(times, dtype=float)
    acceleration = np.asarray(acceleration, dtype=float)
    if (times.ndim != 1 or acceleration.ndim != 1 or times.size != acceleration.size
            or not np.isfinite(times).all() or not np.isfinite(acceleration).all()
            or np.any(np.diff(times) <= 0)):
        raise ValueError("expected finite, equally sized samples with increasing times")
    if max_gap <= 0:
        raise ValueError("max_gap must be positive")
    columns = ["time", "raw_acceleration", "filtered_acceleration", "is_peak", "span"]
    if not times.size:
        return pd.DataFrame(columns=columns)
    spans = np.split(np.arange(times.size), np.flatnonzero(np.diff(times) > max_gap + 1e-9) + 1)
    frames = []
    for span, indices in enumerate(spans):
        ts, raw = times[indices], acceleration[indices]
        filtered = raw.copy()
        peaks = np.array([], dtype=int)
        if len(ts) >= 4 and ts[-1] - ts[0] >= 1:
            step = float(np.median(np.diff(ts)))
            fs = round(1 / step, 8)
            grid = ts[0] + np.arange(int(round((ts[-1] - ts[0]) / step)) + 1) * step
            grid = grid[grid <= ts[-1] + 1e-9]
            raw = np.interp(grid, ts, raw)
            ts = grid
            sos = butter(4, min(5, fs * 0.4), btype="lowpass", fs=fs, output="sos")
            # scipy's default padding for this 4th order low-pass is 15 samples.
            if len(raw) > 15:
                filtered = sosfiltfilt(sos, raw)
                peaks, _ = find_peaks(filtered, distance=max(1, int(fs / 2.5)), prominence=0.7)
            else:
                filtered = raw.copy()
        flags = np.zeros(len(ts), dtype=int)
        flags[peaks] = 1
        frames.append(pd.DataFrame(dict(time=ts, raw_acceleration=raw,
                                       filtered_acceleration=filtered, is_peak=flags, span=span)))
    return pd.concat(frames, ignore_index=True)[columns]


def analyze_imu(records, signal="Magnitude"):
    """Analyze the latest boot only, converting firmware g to m/s²."""
    if records.empty:
        return analyze_acceleration([], [])
    records = records[records["segment"] == records["segment"].iloc[-1]]
    if signal == "Magnitude":
        acceleration = np.linalg.norm(records[["ax_g", "ay_g", "az_g"]].to_numpy(), axis=1)
    else:
        acceleration = records[{"X": "ax_g", "Y": "ay_g", "Z": "az_g"}[signal]].to_numpy()
    return analyze_acceleration(records["time"], acceleration * STANDARD_GRAVITY)
