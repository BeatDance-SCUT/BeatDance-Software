"""Shared acceleration analysis for simulated and received IMU data."""

from dataclasses import asdict, dataclass
import math

import numpy as np
import pandas as pd
from scipy.signal import butter, find_peaks, sosfiltfilt


STANDARD_GRAVITY = 9.80665


@dataclass(frozen=True)
class AlgorithmConfig:
    """Offline baseline parameters; acceleration in m/s² and time in seconds."""

    signal: str = "Magnitude"
    cutoff_hz: float = 5.0
    prominence: float = 0.7
    min_peak_interval: float = 0.4
    max_gap: float = 0.2

    def __post_init__(self):
        if self.signal not in ("Magnitude", "X", "Y", "Z"):
            raise ValueError("unknown signal")
        for name in ("cutoff_hz", "prominence", "min_peak_interval", "max_gap"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a finite positive number")

    def to_dict(self):
        return asdict(self)


def peak_intervals(analysis):
    """Return Δt at the later peak's device-relative time, in seconds.

    Keep signals, device boots and continuous spans separate when those columns
    exist. The first peak in each group has no interval. Ignore zero-length
    intervals from duplicate peak timestamps; never change the input frame.
    """
    groups = [key for key in ("signal", "segment", "span") if key in analysis.columns]
    peaks = analysis.loc[analysis["is_peak"] == 1, [*groups, "time"]].copy()
    peaks = peaks.sort_values([*groups, "time"], kind="stable")
    if groups:
        previous = peaks.groupby(groups, sort=False)["time"].shift()
    else:
        previous = peaks["time"].shift()
    peaks["previous_peak_time"] = previous
    peaks["delta_t"] = peaks["time"] - previous
    return peaks.loc[peaks["delta_t"] > 0].reset_index(drop=True)


def analyze_acceleration(times, acceleration, *, max_gap=None, config=None):
    """Resample each continuous span on device time, then filter and find peaks.

    UDP gaps longer than max_gap seconds are not interpolated or filtered across.
    Short spans (<1 second) are shown raw and are not counted as peaks. Results
    are a retrospective window estimate; edge peaks can change as data arrives.
    """
    config = config or AlgorithmConfig()
    max_gap = config.max_gap if max_gap is None else max_gap
    times = np.asarray(times, dtype=float)
    acceleration = np.asarray(acceleration, dtype=float)
    if (times.ndim != 1 or acceleration.ndim != 1 or times.size != acceleration.size
            or not np.isfinite(times).all() or not np.isfinite(acceleration).all()
            or np.any(np.diff(times) <= 0)):
        raise ValueError("expected finite, equally sized samples with increasing times")
    if isinstance(max_gap, bool) or not math.isfinite(max_gap) or max_gap <= 0:
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
            sos = butter(4, min(config.cutoff_hz, fs * 0.4), btype="lowpass", fs=fs, output="sos")
            # scipy's default padding for this 4th order low-pass is 15 samples.
            if len(raw) > 15:
                filtered = sosfiltfilt(sos, raw)
                peaks, _ = find_peaks(filtered, distance=max(1, int(fs / (1 / config.min_peak_interval))),
                                      prominence=config.prominence)
            else:
                filtered = raw.copy()
        flags = np.zeros(len(ts), dtype=int)
        flags[peaks] = 1
        frames.append(pd.DataFrame(dict(time=ts, raw_acceleration=raw,
                                       filtered_acceleration=filtered, is_peak=flags, span=span)))
    return pd.concat(frames, ignore_index=True)[columns]


def analyze_imu(records, signal=None, *, config=None):
    """Analyze the latest boot only, converting firmware g to m/s²."""
    config = config or AlgorithmConfig()
    signal = config.signal if signal is None else signal
    if signal not in ("Magnitude", "X", "Y", "Z"):
        raise ValueError("unknown signal")
    if records.empty:
        return analyze_acceleration([], [], config=config)
    records = records[records["segment"] == records["segment"].iloc[-1]]
    if signal == "Magnitude":
        acceleration = np.linalg.norm(records[["ax_g", "ay_g", "az_g"]].to_numpy(), axis=1)
    else:
        acceleration = records[{"X": "ax_g", "Y": "ay_g", "Z": "az_g"}[signal]].to_numpy()
    return analyze_acceleration(records["time"], acceleration * STANDARD_GRAVITY, config=config)
