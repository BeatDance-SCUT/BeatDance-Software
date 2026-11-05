"""Offline counting plugins. No labels or evaluation state enter a detector.

All presets share the legacy gap-aware, zero-phase preprocessing. These are
retrospective detectors; event time is NOT online confirmation time.
"""
from dataclasses import dataclass
from collections import deque
import numpy as np
import pandas as pd
from scipy.signal import find_peaks, peak_prominences
from .signal_processing import AlgorithmConfig, analyze_imu

BASELINE = "offline-butterworth-peaks-v1"
PRESETS = {
    BASELINE: "A0 固定参数波峰",
    "adaptive-amplitude-v1": "A1 自适应幅度",
    "adaptive-interval-v1": "A2 自适应间隔",
    "adaptive-both-v1": "A3 幅度与间隔自适应",
    "hysteresis-v1": "B0 迟滞阈值状态机",
}
DEFAULTS = dict(window_seconds=3.0, amplitude_factor=0.8, interval_ratio=0.6,
                min_interval=0.15, max_interval=0.9, high_sigma=0.8, low_sigma=-0.2)

@dataclass
class DetectionResult:
    analysis: pd.DataFrame
    candidates: pd.DataFrame


def validate_params(params):
    if set(params) - set(DEFAULTS):
        raise ValueError("unknown detector parameter")
    values = {**DEFAULTS, **params}
    for key, value in values.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not np.isfinite(value):
            raise ValueError(f"{key} must be finite numeric")
    for key in ("window_seconds", "amplitude_factor", "min_interval", "max_interval", "high_sigma"):
        if values[key] <= 0:
            raise ValueError(f"{key} must be positive")
    if not 0 < values["interval_ratio"] < 1:
        raise ValueError("interval_ratio must be between 0 and 1")
    if values["min_interval"] > values["max_interval"] or values["low_sigma"] >= values["high_sigma"]:
        raise ValueError("invalid threshold bounds")
    return values


def detect(records, config=None, algorithm_id=BASELINE, params=None):
    """Return sample traces and candidate decisions; events are is_peak==1.

    Legacy ingestion uses g. analyze_imu converts to m/s². Each continuous span
    resets thresholds, refractory history and FSM. Only preprocessing uses future
    data; adaptation uses a trailing window. No cross-gap interval estimation.
    """
    config = config or AlgorithmConfig()
    if algorithm_id not in PRESETS:
        raise ValueError("unknown algorithm_id")
    p = validate_params(params or {})
    analysis = analyze_imu(records, config=config).copy()
    decisions = []
    if algorithm_id == BASELINE or analysis.empty:
        return DetectionResult(analysis, pd.DataFrame(columns=["time", "span", "accepted", "reason"]))
    analysis["is_peak"] = 0
    for span, group in analysis.groupby("span", sort=False):
        idx = group.index.to_numpy()
        t, y = group.time.to_numpy(), group.filtered_acceleration.to_numpy()
        if len(t) <= 15 or t[-1] - t[0] < 1:
            continue
        window = max(3, int(round(p["window_seconds"] / np.median(np.diff(t)))))
        series = pd.Series(y)
        center = series.rolling(window, min_periods=3).median().to_numpy()
        # Trailing robust amplitude, with a physical floor for static/noisy input.
        scale = series.rolling(window, min_periods=3).apply(
            lambda a: 1.4826 * np.median(np.abs(a - np.median(a))), raw=True).to_numpy()
        scale = np.maximum(np.nan_to_num(scale, nan=0.0), 1e-6)
        center = np.nan_to_num(center, nan=float(y[0]))
        adaptive_amp = algorithm_id in ("adaptive-amplitude-v1", "adaptive-both-v1")
        threshold = np.maximum(0.05, p["amplitude_factor"] * scale) if adaptive_amp else np.full(len(t), config.prominence)
        high, low = center + p["high_sigma"] * scale, center + p["low_sigma"] * scale
        analysis.loc[idx, "threshold"] = threshold
        analysis.loc[idx, "high_threshold"] = high
        analysis.loc[idx, "low_threshold"] = low
        candidates = []
        if algorithm_id == "hysteresis-v1":
            active, best = False, 0
            for i in range(2, len(t)):
                if not active and y[i] > high[i] and y[i-1] <= high[i-1] and high[i]-low[i] >= 0.05:
                    active, best = True, i
                if active:
                    if y[i] > y[best]:
                        best = i
                    if y[i] <= low[i]:
                        candidates.append((best, t[i], True))
                        active = False
        else:
            peaks, _ = find_peaks(y)
            prominence = peak_prominences(y, peaks)[0]
            candidates = [(int(i), t[i], bool(v >= threshold[i])) for i, v in zip(peaks, prominence)]
        accepted = []
        recent_candidates = deque()
        for i, confirmed, amplitude_ok in candidates:
            interval = config.min_peak_interval
            if algorithm_id in ("adaptive-interval-v1", "adaptive-both-v1"):
                # Start permissively; estimate from raw candidate spacings, rather
                # than feeding accepted-event misses back into the refractory gate.
                while recent_candidates and recent_candidates[0] < t[i]-p["window_seconds"]:
                    recent_candidates.popleft()
                gaps = np.diff(list(recent_candidates) + [t[i]])
                plausible = gaps[(gaps >= p["min_interval"]) & (gaps <= p["max_interval"] / p["interval_ratio"])]
                interval = p["min_interval"] if not len(plausible) else float(np.clip(
                    p["interval_ratio"] * np.median(plausible), p["min_interval"], p["max_interval"]))
            if amplitude_ok:
                recent_candidates.append(t[i])
            ok = amplitude_ok and (not accepted or t[i]-accepted[-1] >= interval-1e-12)
            reason = "accepted" if ok else ("amplitude" if not amplitude_ok else "refractory")
            decisions.append(dict(time=float(t[i]), span=int(span), accepted=bool(ok), reason=reason,
                                  refractory_seconds=interval, decision_sample_time=float(confirmed)))
            if ok:
                accepted.append(t[i])
                analysis.loc[idx[i], "is_peak"] = 1
    return DetectionResult(analysis, pd.DataFrame(decisions, columns=["time", "span", "accepted", "reason", "refractory_seconds", "decision_sample_time"]))
