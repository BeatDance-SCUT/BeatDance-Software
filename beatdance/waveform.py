"""Causal waveform period and phase tracking."""

from collections import deque
from dataclasses import dataclass
import math

import numpy as np


SAMPLE_STEP = 0.02
ANALYSIS_STEP = 0.08
WINDOW_RADIUS = 0.92
MIN_PERIOD = 0.28
MAX_PERIOD = 0.82
AUTO_MIN_MOTION_G = 0.38
AUTO_MIN_CORRELATION = 0.43

_SAMPLE_LAGS = np.arange(round(MIN_PERIOD / SAMPLE_STEP), round(MAX_PERIOD / SAMPLE_STEP) + 1)
_TAU = 2 * np.pi


def _period(window, previous):
    """Compare whole waveforms at all allowed lags."""
    available = min(len(window) // 2, _SAMPLE_LAGS[-1])
    lags = _SAMPLE_LAGS[_SAMPLE_LAGS <= available]
    if len(lags) < 3:
        return None

    values = window - window.mean(axis=0)
    # Magnitude and the gyro vector each contribute half the total energy.
    scale = np.sqrt(np.mean(values ** 2, axis=0))
    values[:, 0] /= max(scale[0], 0.10)
    values[:, 1:] /= max(float(np.linalg.norm(scale[1:])), 20.0)
    similarities = []
    for lag in lags:
        before, after = values[:-lag], values[lag:]
        energy = np.linalg.norm(before) * np.linalg.norm(after)
        similarities.append(float(np.sum(before * after) / max(energy, 1e-9)))

    correlation = np.asarray(similarities)
    # A repeat at twice the actual period can have a similarly high score.
    # Favor the shorter lag and gently penalize sudden changes in cadence.
    scores = correlation - 0.13 * (lags - lags[0]) / (_SAMPLE_LAGS[-1] - lags[0])
    if previous is not None:
        scores -= 0.22 * abs(np.log(lags * SAMPLE_STEP / previous))
    peak = int(np.argmax(scores))
    lag = float(lags[peak])
    if 0 < peak < len(lags) - 1:
        a, b, c = scores[peak - 1:peak + 2]
        if a - 2 * b + c < -1e-9:
            lag += float(np.clip((a - c) / (2 * (a - 2 * b + c)), -0.5, 0.5))
    return lag * SAMPLE_STEP, float(correlation[peak])


def _estimate_phase(times, window, center, period):
    """Fit the magnitude's fundamental, whose crest defines zero phase."""
    offsets = times - center
    angles = _TAU * offsets / period
    weights = np.sqrt(np.maximum(0.12, 1 - abs(offsets) / WINDOW_RADIUS))
    design = np.column_stack((np.ones(len(times)), np.cos(angles), np.sin(angles)))
    _, cosine, sine = np.linalg.lstsq(design * weights[:, None], window[:, 0] * weights, rcond=None)[0]
    return float(np.arctan2(-sine, cosine) / _TAU)


def _phase_crossing(time, phase, period, previous):
    """Unwrap the current phase and locate a crossing since the previous hop."""
    if previous is None:
        event = time - phase * period
        return phase, event if time - ANALYSIS_STEP <= event <= time else None

    last_time, last_phase, last_period = previous
    expected = (time - last_time) / ((period + last_period) / 2)
    phase += round(last_phase + expected - phase)
    crossing = np.floor(last_phase) + 1
    if last_phase < crossing <= phase:
        fraction = (crossing - last_phase) / (phase - last_phase)
        return phase, last_time + fraction * (time - last_time)
    return phase, None


_EPSILON = 1e-9
_HOP = round(ANALYSIS_STEP / SAMPLE_STEP)
_HISTORY = 2 * round(WINDOW_RADIUS / SAMPLE_STEP) + 1
_MOTION = 2 * round(0.20 / SAMPLE_STEP) + 1
_MIN_EVENT_SPACING = 0.8 * MIN_PERIOD


@dataclass(frozen=True)
class WaveformHop:
    span: int
    time_s: float
    available_s: float
    period_s: float | None
    correlation: float | None
    motion_g: float
    event_s: float | None


class WaveformCounter:
    """Evaluate a trailing waveform when a raw packet closes a grid interval."""

    def __init__(self, *, max_gap=0.20):
        self._max_gap = max_gap
        self.segment = None
        self.span = -1
        self._raw = None
        self._history = deque(maxlen=_HISTORY)
        self._phase = None
        self._last_event = None

    def push(self, time, ax, ay, az, gx, gy, gz, *, segment=0):
        if not all(map(math.isfinite, (time, ax, ay, az, gx, gy, gz))):
            raise ValueError("expected finite device time and six axes")
        value = np.array((np.linalg.norm((ax, ay, az)), gx, gy, gz))
        if not np.isfinite(value).all():
            raise ValueError("acceleration norm must be finite")

        if self.segment != segment or self._raw is None or time - self._raw[0] > self._max_gap + _EPSILON:
            self.segment = segment
            self.span += 1
            self._history.clear()
            self._phase = self._last_event = None
            self._first_tick = self._tick = math.ceil((time - _EPSILON) / SAMPLE_STEP)
            self._raw = (time, value)
        elif time <= self._raw[0]:
            raise ValueError("device time must increase within each segment")

        previous_time, previous_value = self._raw
        last_tick = math.floor((time + _EPSILON) / SAMPLE_STEP)
        hops = []
        for tick in range(self._tick, last_tick + 1):
            grid_time = tick * SAMPLE_STEP
            if grid_time <= previous_time:
                interpolated = previous_value
            elif grid_time >= time:
                interpolated = value
            else:
                interpolated = (value - previous_value) / (time - previous_time) * (grid_time - previous_time) + previous_value
            self._history.append((grid_time, *interpolated))
            if (tick - self._first_tick) % _HOP == 0:
                hops.append(self._analyse(grid_time, time))
        self._tick = last_tick + 1
        self._raw = (time, value)
        return hops

    def _analyse(self, time, available):
        data = np.asarray(self._history)
        estimate = _period(data[:, 1:], self._phase[2] if self._phase is not None else None)
        motion = float(np.std(data[-_MOTION:, 1]))
        correlation = estimate[1] if estimate is not None else None
        if estimate is None or motion < AUTO_MIN_MOTION_G or correlation < AUTO_MIN_CORRELATION:
            self._phase = None
            return WaveformHop(self.span, time, available, None, correlation, motion, None)

        period, correlation = estimate
        phase = _estimate_phase(data[:, 0], data[:, 1:], time, period)
        phase, event = _phase_crossing(time, phase, period, self._phase)
        self._phase = time, phase, period
        if event is not None and self._first_tick * SAMPLE_STEP <= event <= time and (
                self._last_event is None or event - self._last_event >= _MIN_EVENT_SPACING):
            self._last_event = event
            return WaveformHop(self.span, time, available, period, correlation, motion, float(event))
        return WaveformHop(self.span, time, available, period, correlation, motion, None)
