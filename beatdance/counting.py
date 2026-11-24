"""Public streaming counter factory and replay adapter for Loss Longer."""

from dataclasses import asdict, dataclass, fields

import numpy as np
import pandas as pd

from .landing import Decision, LandingModel
from .loss_longer import DeferredConfig, DeferredCounter


ALGORITHM_ID = "loss_longer"
DEFAULTS = asdict(DeferredConfig())
# Frozen three-feature model used by the selected development replay.
MODEL = LandingModel(
    center=(18.356777479583332, 628.8037232988715, 203.6900841267501),
    scale=(7.430974498913094, 341.53926959124965, 101.53540256124815),
    weights=(0.2893997698692087, 1.0845489920084026, -0.7779400791350621),
    intercept=0.7036806782197101,
)
AXES = ("ax_g", "ay_g", "az_g", "gx_dps", "gy_dps", "gz_dps")
ANALYSIS_COLUMNS = ["segment", "time", "raw_acceleration", "filtered_acceleration",
                    "is_peak", "span", "emitted_s", "source", "count", "period_s"]
CANDIDATE_COLUMNS = [field.name for field in fields(Decision)]


def validate_params(params):
    if not isinstance(params, dict) or set(params) - set(DEFAULTS):
        raise ValueError("unknown Loss Longer parameter")
    for key, value in params.items():
        if isinstance(DEFAULTS[key], bool):
            if not isinstance(value, bool):
                raise ValueError(f"{key} must be boolean")
        elif isinstance(value, bool) or not isinstance(value, (int, float)) or not np.isfinite(value):
            raise ValueError(f"{key} must be finite numeric")
    return DeferredConfig(**params)


def new_counter(params=None):
    return DeferredCounter(MODEL, config=validate_params({} if params is None else params))


def algorithm_metadata():
    return {"signal": "Magnitude", "max_gap": new_counter().landings.config.max_gap_s}


@dataclass
class DetectionResult:
    analysis: pd.DataFrame
    candidates: pd.DataFrame


def detect(records, params=None):
    """Replay the stream and mark events at their exact estimated device times.

    Publish time and count are recorded independently of the plot coordinate.
    Keep the input row order within each boot and close a complete input once.
    """
    counter = new_counter(params)
    rows, candidates, events = [], [], []
    if not records.empty:
        for segment, time, *axes in records[["segment", "time", *AXES]].itertuples(index=False, name=None):
            events.extend(counter.push(float(time), *map(float, axes), segment=int(segment)))
            sample = counter.landings.previous
            rows.append((int(segment), sample.time, sample.raw, sample.filtered, counter.waveform.span))
            candidates.extend(asdict(d) for d in counter.last_decisions)
        events.extend(counter.finish())
    frames = []
    raw = pd.DataFrame(rows, columns=["segment", "time", "raw_acceleration", "filtered_acceleration", "span"])
    # A same-boot gap may settle the preceding span on the next packet.
    by_span = {}
    for event in events:
        by_span.setdefault(event.span, []).append(event)
    for span, group in raw.groupby("span", sort=False):
        marks = by_span.get(span, ())
        marked_times = [e.time_s for e in marks]
        native = group.time.to_numpy()
        extra = np.setdiff1d(marked_times, native)
        if len(extra):
            group = pd.concat([group, pd.DataFrame({
                "segment": group.segment.iloc[0], "time": extra, "span": span,
                "raw_acceleration": np.interp(extra, native, group.raw_acceleration),
                "filtered_acceleration": np.interp(extra, native, group.filtered_acceleration),
            })], ignore_index=True).sort_values("time", kind="stable")
        if marks:
            group = group.merge(pd.DataFrame({
                "time": marked_times, "emitted_s": [e.emitted_s for e in marks],
                "source": [e.source for e in marks], "count": [e.count for e in marks],
                "period_s": [e.period_s for e in marks],
            }), on="time", how="left", validate="one_to_one")
            group["is_peak"] = group.emitted_s.notna().astype(int)
        else:
            group = group.assign(is_peak=0)
        frames.append(group)
    analysis = (pd.concat(frames, ignore_index=True) if frames else
                pd.DataFrame(columns=ANALYSIS_COLUMNS)).reindex(columns=ANALYSIS_COLUMNS)
    return DetectionResult(analysis, pd.DataFrame(candidates, columns=CANDIDATE_COLUMNS))
