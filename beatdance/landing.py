"""Causal landing candidate features and decisions for Loss Longer."""

from collections import deque
from dataclasses import dataclass
from math import exp, expm1, hypot, isfinite, pi
from statistics import median


GRAVITY = 9.80665
EPSILON = 1e-9


@dataclass(frozen=True)
class LandingModel:
    center: tuple[float, float, float]
    scale: tuple[float, float, float]
    weights: tuple[float, float, float]
    intercept: float

    def __post_init__(self):
        if any(len(values) != 3 or not all(map(isfinite, values))
               for values in (self.center, self.scale, self.weights)):
            raise ValueError("the landing model must have three finite inputs")
        if min(self.scale) <= 0 or not isfinite(self.intercept):
            raise ValueError("invalid landing model normalization")

    def score(self, features):
        value = self.intercept + sum(weight * (feature - center) / scale
                                    for feature, center, scale, weight in
                                    zip(features, self.center, self.scale, self.weights, strict=True))
        tail = exp(-abs(value))
        return 1 / (1 + tail) if value >= 0 else tail / (1 + tail)


@dataclass(frozen=True)
class LandingConfig:
    wait_s: float = 0.12
    cutoff_hz: float = 5.0
    history_s: float = 3.0
    before_s: float = 0.14
    max_gap_s: float = 0.20
    noise_floor: float = 0.16
    noise_factor: float = 0.12
    min_rise: float = 0.30
    min_jerk: float = 35.0
    high_score: float = 0.54
    low_score: float = 0.31
    recovery_fraction: float = 0.56
    strong_drop: float = 0.20
    weak_drop: float = 0.38

    def __post_init__(self):
        for value in vars(self).values():
            if isinstance(value, bool) or not isfinite(value) or value <= 0:
                raise ValueError("C3 parameters must be finite and positive")
        if not self.wait_s < self.max_gap_s or self.before_s > self.history_s:
            raise ValueError("C3 candidate windows exceed the configured limits")
        if not self.low_score < self.high_score < 1:
            raise ValueError("invalid C3 score thresholds")
        if not self.strong_drop <= self.weak_drop <= self.recovery_fraction < 1:
            raise ValueError("invalid C3 recovery fractions")


@dataclass(frozen=True)
class Sample:
    time: float
    raw: float
    filtered: float
    gyro: float
    threshold: float


@dataclass
class Candidate:
    peak: Sample
    rise: float
    jerk: float
    gyro: float
    score: float
    post_min: float

    @property
    def drop(self):
        return (self.peak.filtered - self.post_min) / self.rise


@dataclass(frozen=True)
class Decision:
    segment: int
    span: int
    time: float
    deadline: float
    decision_sample_time: float
    evidence_through_time: float
    accepted: bool
    reason: str
    rise_m_s2: float
    jerk_m_s3: float
    gyro_change_dps: float
    score: float
    drop_fraction: float
    threshold_m_s2: float


class LandingDetector:
    """State for one stream. Counts persist across gaps and device boots."""

    def __init__(self, model, config=None):
        self.model = model
        self.config = config or LandingConfig()
        self.span = -1
        self.segment = None
        self._reset_waveform()

    def _reset_waveform(self):
        self.history = deque()
        self.previous = None
        self.turning_up_since = None
        self.group = []
        self.closed_impulse = None

    def push(self, time, ax, ay, az, gx, gy, gz, *, segment=0):
        """Accept g and deg/s. Return this sample's trace and final decisions.

        A packet beyond a candidate's deadline can only record its expiry.
        A gap discards pending peaks before resetting the candidate detector.
        """
        if not all(map(isfinite, (time, ax, ay, az, gx, gy, gz))):
            raise ValueError("C3 requires finite device time and six axes")
        raw, gyro = hypot(ax, ay, az) * GRAVITY, hypot(gx, gy, gz)
        if not isfinite(raw) or not isfinite(gyro):
            raise ValueError("C3 magnitudes must be finite")
        config = self.config
        decisions = []
        previous = self.previous
        new_segment = self.segment != segment
        if not new_segment and previous and time <= previous.time:
            raise ValueError("device time must increase within each segment")
        if new_segment or (previous and time - previous.time > config.max_gap_s + EPSILON):
            decisions.extend(self.end("segment_change" if new_segment else "data_gap"))
            self.segment = segment
            self.span += 1
            previous = None
        while self.group and time > self.group[0].peak.time + config.wait_s + EPSILON:
            first = self.group.pop(0)
            decisions.append(self._decision(first, time, previous.time, False, "candidate_expired"))

        while self.history and self.history[0].time < time - config.history_s - EPSILON:
            self.history.popleft()
        filtered = raw
        if previous:
            alpha = -expm1(-2 * pi * config.cutoff_hz * (time - previous.time))
            filtered = previous.filtered + alpha * (raw - previous.filtered)
        values = [sample.filtered for sample in self.history]
        baseline = median(values) if values else filtered
        noise = 1.4826 * median(abs(value - baseline) for value in values) if values else 0.0
        threshold = max(config.noise_floor, config.noise_factor * noise)
        sample = Sample(time, raw, filtered, gyro, threshold)

        if previous and filtered < previous.filtered - EPSILON and self.turning_up_since is not None:
            candidate = self._make_candidate(previous)
            self.turning_up_since = None
            if candidate and time > previous.time + config.wait_s + EPSILON:
                decisions.append(self._decision(candidate, time, previous.time, False, "late_peak_detection"))
            elif candidate and self.closed_impulse is not None:
                candidate.post_min = filtered
                decisions.append(self._decision(candidate, time, time, False, "same_impulse"))
            elif candidate:
                self.group.append(candidate)
        elif previous and filtered > previous.filtered + EPSILON and self.turning_up_since is None:
            self.turning_up_since = previous.time

        if self.closed_impulse is not None and filtered <= self.closed_impulse:
            self.closed_impulse = None
        if self.group:
            for candidate in self.group:
                candidate.post_min = min(candidate.post_min, filtered)
            first = self.group[0]
            if first.drop >= config.recovery_fraction or time >= first.peak.time + config.wait_s - EPSILON:
                decisions.extend(self._resolve(time, time))

        self.history.append(sample)
        self.previous = sample
        return sample, decisions

    def end(self, reason="end_of_input"):
        """Abandon unfinished candidates at stream boundaries, preserving the count."""
        decisions = [self._decision(candidate, self.previous.time, self.previous.time, False, reason)
                     for candidate in self.group]
        self._reset_waveform()
        self.segment = None
        return decisions

    def _make_candidate(self, peak):
        window = [sample for sample in reversed(self.history)
                  if peak.time - self.config.before_s - EPSILON <= sample.time]
        valley = min(sample.filtered for sample in window if sample.time >= self.turning_up_since)
        rise = peak.filtered - valley
        if rise < peak.threshold:
            return None
        jerk = max((later.raw - earlier.raw) / (later.time - earlier.time)
                   for later, earlier in zip(window, window[1:]))
        jerk = max(0.0, jerk)
        gyro = max(sample.gyro for sample in window) - min(sample.gyro for sample in window)
        return Candidate(peak, rise, jerk, gyro, self.model.score((rise, jerk, gyro)), peak.filtered)

    def _resolve(self, time, evidence_time):
        config = self.config
        eligible = []
        for candidate in self.group:
            if candidate.rise < config.min_rise or candidate.jerk < config.min_jerk:
                reason = "weak_acceleration"
            elif candidate.score >= config.high_score and candidate.drop >= config.strong_drop:
                reason = "strong_landing"
            elif candidate.score < config.low_score:
                reason = "low_landing_score"
            elif candidate.drop < config.weak_drop:
                reason = "insufficient_recovery"
            else:
                reason = "no_landing_support"
            eligible.append((candidate, reason))
        winners = [(candidate, reason) for candidate, reason in eligible
                   if reason == "strong_landing"]
        winner = None
        if winners:
            winner, _ = max(winners, key=lambda pair: pair[0].score)
            first = self.group[0]
            if first.drop < config.recovery_fraction:
                self.closed_impulse = first.peak.filtered - config.recovery_fraction * first.rise
        decisions = [self._decision(candidate, time, evidence_time, candidate is winner,
                                    "competing_peak" if winner is not None and candidate is not winner else reason)
                     for candidate, reason in eligible]
        self.group.clear()
        return decisions

    def _decision(self, candidate, time, evidence_time, accepted, reason):
        peak = candidate.peak
        return Decision(self.segment, self.span, peak.time, peak.time + self.config.wait_s,
                        time, evidence_time, accepted, reason, candidate.rise,
                        candidate.jerk, candidate.gyro, candidate.score, candidate.drop, peak.threshold)
