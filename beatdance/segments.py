"""Activity segmentation over detected movement events.

Splits a sequence of detected events into continuous activity segments and
drops fragments that are too short to be a real set of repetitions. This is
the offline counterpart of the runtime start/stop state machine: it answers
"was the user actually moving during this stretch?" rather than "did the
signal cross a threshold here?".

Why it is a separate module: peak detection reports *every* local maximum,
including ones produced while the user is resting, adjusting the rope or
talking. Counting them inflates the repetition count, and the long intervals
they create also wreck cycle-variability statistics. Segmenting first, then
counting and measuring only inside retained segments, is what fixes both.

Known limitation
----------------
When ``reference_period`` is not supplied, the split threshold is derived from
the median interval of the events themselves. That makes the rule depend on the
detector: if detection locks onto every second repetition, the median roughly
doubles and the threshold becomes too large to cut the real rest gaps. Callers
that already have a period estimate (from autocorrelation, or from a calibrated
baseline) should pass it explicitly to break that dependency.
"""

from dataclasses import asdict, dataclass
import math

import numpy as np
import pandas as pd


SEGMENT_COLUMNS = [
    "span",
    "start_time",
    "end_time",
    "first_index",
    "last_index",
    "n_events",
    "n_cycles",
    "period",
    "threshold",
    "is_retained",
]


@dataclass(frozen=True)
class ActivityConfig:
    """Segmentation thresholds; times in seconds, periods in seconds."""

    # 间隔超过 gap_factor × 参考周期即视为中断。取 2.5 是因为正常动作的
    # 周期抖动一般小于 30%，而一次真正的停顿至少漏掉一拍以上。
    gap_factor: float = 2.5
    # 一段里不足 min_cycles 个完整周期就判为干扰（调整绳子、单次试探）。
    min_cycles: int = 3

    def __post_init__(self):
        if (isinstance(self.gap_factor, bool)
                or not isinstance(self.gap_factor, (int, float))
                or not math.isfinite(self.gap_factor)
                or self.gap_factor <= 1):
            raise ValueError("gap_factor must be a finite number greater than 1")
        if (isinstance(self.min_cycles, bool)
                or not isinstance(self.min_cycles, int)
                or self.min_cycles < 1):
            raise ValueError("min_cycles must be a positive integer")

    def to_dict(self):
        return asdict(self)


def _prepare(times, spans, reference_period):
    times = np.asarray(times, dtype=float)
    if times.ndim != 1:
        raise ValueError("event times must be one-dimensional")
    if not np.isfinite(times).all():
        raise ValueError("event times must be finite")

    size = times.size
    if spans is None:
        spans = np.zeros(size, dtype=int)
    else:
        spans = np.asarray(spans)
        if spans.ndim != 1 or spans.size != size:
            raise ValueError("spans must match times in length")
        spans = spans.astype(int, copy=False)

    if reference_period is not None:
        if (isinstance(reference_period, bool)
                or not isinstance(reference_period, (int, float))
                or not math.isfinite(reference_period)
                or reference_period <= 0):
            raise ValueError("reference_period must be a positive finite number")

    return times, spans


def split_active_segments(times, spans=None, *, config=None, reference_period=None):
    """Return one row per candidate activity segment, including dropped ones.

    Candidate segments are formed inside each ``span`` separately, so a device
    reboot or a sampling gap never merges two unrelated stretches. A segment is
    retained when it holds at least ``config.min_cycles`` complete cycles.

    ``first_index`` and ``last_index`` are positions in the *input* arrays, so
    the result can be used to index back into the caller's event table.

    ``is_retained`` is reported instead of silently dropping rows, so callers
    can show why a fragment was rejected (the dashboard already surfaces failed
    segments this way).
    """
    config = config or ActivityConfig()
    times, spans = _prepare(times, spans, reference_period)

    if times.size == 0:
        return pd.DataFrame(columns=SEGMENT_COLUMNS)

    order = np.argsort(times, kind="stable")
    sorted_times = times[order]
    sorted_spans = spans[order]

    rows = []
    for span in np.unique(sorted_spans):
        positions = np.flatnonzero(sorted_spans == span)
        group_times = sorted_times[positions]
        intervals = np.diff(group_times)

        if reference_period is not None:
            period = float(reference_period)
        elif intervals.size:
            period = float(np.median(intervals))
        else:
            period = 0.0

        threshold = config.gap_factor * period if period > 0 else 0.0
        if intervals.size and threshold > 0:
            cuts = np.flatnonzero(intervals > threshold) + 1
        else:
            cuts = np.array([], dtype=int)

        for run in np.split(np.arange(group_times.size), cuts):
            first = int(order[positions[run[0]]])
            last = int(order[positions[run[-1]]])
            cycles = int(run.size - 1)
            rows.append(dict(
                span=int(span),
                start_time=float(group_times[run[0]]),
                end_time=float(group_times[run[-1]]),
                first_index=first,
                last_index=last,
                n_events=int(run.size),
                n_cycles=cycles,
                period=period,
                threshold=threshold,
                is_retained=cycles >= config.min_cycles,
            ))

    return pd.DataFrame(rows, columns=SEGMENT_COLUMNS)


def active_mask(times, spans=None, *, config=None, reference_period=None):
    """Boolean mask over the input events, True inside a retained segment.

    Intended for the common call pattern::

        keep = active_mask(peaks["time"], peaks["span"])
        peaks = peaks.loc[keep]

    which keeps counting and stability metrics inside real activity only.
    """
    result = split_active_segments(times, spans, config=config,
                                   reference_period=reference_period)
    times, spans = _prepare(times, spans, reference_period)
    mask = np.zeros(times.size, dtype=bool)

    for row in result.itertuples(index=False):
        if not row.is_retained:
            continue
        inside = ((spans == row.span)
                  & (times >= row.start_time)
                  & (times <= row.end_time))
        mask |= inside

    return mask
