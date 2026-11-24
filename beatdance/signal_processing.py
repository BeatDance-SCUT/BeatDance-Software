"""Single-axis display traces and interval calculation for saved counts."""

import numpy as np
import pandas as pd

from .landing import GRAVITY


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


def axis_trace(frame, signal, *, max_gap=0.20, cutoff_hz=5.0):
    raw = frame[f"a{signal.lower()}_g"].to_numpy(dtype=float) * GRAVITY
    time = frame.time.to_numpy(dtype=float)
    steps = np.diff(time, prepend=time[0] if len(time) else 0)
    breaks = (steps > max_gap + 1e-9) | (steps < 0)
    filtered = raw.copy()
    for i in range(1, len(raw)):
        if not breaks[i]:
            alpha = -np.expm1(-2 * np.pi * cutoff_hz * steps[i])
            filtered[i] = filtered[i-1] + alpha * (raw[i] - filtered[i-1])
    return pd.DataFrame(dict(segment=frame.segment.to_numpy(), time=time, raw_acceleration=raw,
                             filtered_acceleration=filtered, is_peak=np.zeros(len(time), dtype=int),
                             span=np.cumsum(breaks)))
