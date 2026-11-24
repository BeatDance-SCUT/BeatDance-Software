"""Causal jump counting with bounded review at startup and loss of phase."""

from collections import deque
from dataclasses import dataclass
from itertools import combinations
from math import isfinite
from statistics import median

from .landing import LandingDetector
from .waveform import MAX_PERIOD, MIN_PERIOD, WaveformCounter


_EPSILON = 1e-9
_SPACING = 0.8 * MIN_PERIOD
_REVIEWABLE = {"no_landing_support", "low_landing_score", "insufficient_recovery", "weak_acceleration"}


@dataclass(frozen=True)
class DeferredConfig:
    startup_wait_s: float = 1.65
    startup_idle_s: float = 0.85
    startup_wave_periods: float = 1.20
    interval_tolerance: float = 0.30
    weak_score: float = 0.38
    weak_drop: float = 0.36
    weak_rise: float = 0.30
    weak_jerk: float = 35.0
    offset_s: float = 0.0
    loss_trigger_periods: float = 1.50
    hold_periods: float = 3.10
    max_loss_wait_s: float = 2.00
    allow_weak_chain: bool = False
    allow_unbracketed_strong: bool = True
    allow_short_pair: bool = True
    pair_min_score: float = 0.62
    pair_requires_wave: bool = False
    settle_on_gap: bool = True

    def __post_init__(self):
        positive = (self.startup_wait_s, self.startup_idle_s, self.startup_wave_periods,
                    self.weak_rise, self.weak_jerk, self.max_loss_wait_s)
        if any(not isfinite(value) or value <= 0 for value in positive):
            raise ValueError("review windows and landing thresholds must be finite and positive")
        if not (0 < self.interval_tolerance < 0.5 and 0 < self.weak_score < 1 and 0 < self.pair_min_score < 1
                and 0 < self.weak_drop < 1 and isfinite(self.offset_s)):
            raise ValueError("invalid weak evidence or interval thresholds")
        if not (1 < self.loss_trigger_periods < self.hold_periods and isfinite(self.hold_periods)):
            raise ValueError("loss review must end after its trigger")


@dataclass(frozen=True)
class CountEvent:
    span: int
    time_s: float
    emitted_s: float
    source: str
    count: int
    period_s: float | None = None



@dataclass(frozen=True)
class _Evidence:
    time_s: float
    wave: bool
    strong: bool
    score: float = 0.0
    period_s: float | None = None


class DeferredCounter:
    """Publish ordered, immutable counts using only packets already received.

    `push` returns the events confirmed by that packet. `finish` settles an
    explicit end of input once, using only decisions made by the last packet.
    A same-clock gap can settle the preceding span using its own evidence.
    A device clock reset opens a new span and drops unsettled evidence.
    """

    def __init__(self, landing_model, landing_config=None, *, config=None, recover_on_loss=True):
        self.config = config or DeferredConfig()
        self.landings = LandingDetector(landing_model, landing_config)
        self.waveform = WaveformCounter(max_gap=self.landings.config.max_gap_s)
        self.recover_on_loss = recover_on_loss
        self.count = 0
        self._time = None
        self._segment = None
        self._span = -1
        self._finished = False
        self.last_decisions = ()
        self._reset_span()

    def _reset_span(self):
        self._pending = []
        self._startup_waves = []
        self._recent_startup = deque(maxlen=4)
        self._wave_intervals = deque(maxlen=3)
        self._last_wave = None
        self._last_event = None
        self._holding = False

    def push(self, time, ax, ay, az, gx, gy, gz, *, segment=0):
        if self._finished:
            raise RuntimeError("cannot push after finish")
        hops = self.waveform.push(time, ax, ay, az, gx, gy, gz, segment=segment)
        _, decisions = self.landings.push(time, ax, ay, az, gx, gy, gz, segment=segment)
        self.last_decisions = tuple(decisions)
        events = []
        old_time = self._time
        self._time = time
        if self.waveform.span != self._span:
            if self.config.settle_on_gap and old_time is not None and self._segment == segment:
                self._settle_end(events)
            self._reset_span()
            self._span = self.waveform.span
            self._segment = segment
        if self._last_wave is not None and self._at_hold_deadline(time):
            self._close_hold(events)
        if self._last_wave is None or self.recover_on_loss:
            self._pending.extend(candidate for decision in decisions
                                 if (candidate := self._candidate(decision)) is not None)
            self._pending.sort(key=lambda candidate: candidate.time_s)

        for hop in hops:
            if hop.event_s is not None:
                self._observe_wave(_Evidence(hop.event_s, True, True, period_s=hop.period_s), events)
            elif self._last_wave is not None and hop.period_s is None:
                self._holding = True

        if self._last_wave is None:
            self._review_startup(events)
        elif time - self._last_wave.time_s >= self.config.loss_trigger_periods * self._period():
            self._holding = True
        return tuple(events)

    def finish(self):
        if self._finished:
            return ()
        self._finished = True
        events = []
        self._settle_end(events)
        self.landings.end()
        return tuple(events)

    def _settle_end(self, events):
        if self._last_wave is None:
            self._review_startup(events, finishing=True)
        elif self.recover_on_loss and self._time is not None:
            self._recover_loss(None, events)

    def _candidate(self, decision):
        if decision.span != self.waveform.span:
            return None
        if decision.accepted:
            return _Evidence(decision.time, False, True, decision.score)
        config = self.config
        if (decision.reason in _REVIEWABLE and decision.score >= config.weak_score
                and decision.drop_fraction >= config.weak_drop and decision.rise_m_s2 >= config.weak_rise
                and decision.jerk_m_s3 >= config.weak_jerk):
            return _Evidence(decision.time, False, False, decision.score)
        return None

    def _phase_time(self, candidate):
        return candidate.time_s if candidate.wave else candidate.time_s + self.config.offset_s

    def _near(self, interval, period):
        return abs(interval - period) <= self.config.interval_tolerance * period + _EPSILON

    def _overlap(self, first, second):
        return (abs(first.time_s - second.time_s) < _SPACING - _EPSILON
                or abs(self._phase_time(first) - self._phase_time(second)) < _SPACING - _EPSILON)

    def _candidate_order(self, candidate):
        return not candidate.wave, not candidate.strong, -candidate.score, candidate.time_s

    def _remove_duplicates(self, candidates, existing=()):
        selected = list(existing)
        added = []
        for candidate in sorted(candidates, key=self._candidate_order):
            if not any(self._overlap(candidate, other) for other in selected):
                added.append(candidate)
                selected.append(candidate)
        return sorted(added, key=lambda candidate: candidate.time_s)

    def _startup_support(self, evidence, subject):
        """Return the strongest observed adjacent triple that contains this peak."""
        best = None
        phase = self._phase_time(subject)
        neighbors = [candidate for candidate in evidence if candidate != subject
                     and abs(self._phase_time(candidate) - phase) <= 2 * MAX_PERIOD + _EPSILON]
        for first, second in combinations(neighbors, 2):
            chain = sorted((first, second, subject), key=self._phase_time)
            a, b, c = map(self._phase_time, chain)
            one, two = b - a, c - b
            if not (MIN_PERIOD <= one <= MAX_PERIOD and MIN_PERIOD <= two <= MAX_PERIOD):
                continue
            period = (one + two) / 2
            strong = sum(candidate.strong for candidate in chain)
            if (not strong and not self.config.allow_weak_chain) or not self._near(one, two):
                continue
            if any(candidate.wave and not self._near(period, candidate.period_s) for candidate in chain):
                continue
            quality = (sum(candidate.wave for candidate in chain), strong, -abs(one - two) / period)
            if best is None or quality > best[0]:
                best = quality, period
        return best

    def _review_startup(self, events, *, finishing=False):
        if not self._pending and not self._startup_waves:
            if finishing or (self._recent_startup and
                    self._time - self._recent_startup[-1].time_s >= self.config.startup_idle_s):
                self._recent_startup.clear()
            return
        self._review_confirmed_startup(events, finishing)

    def _review_confirmed_startup(self, events, finishing):
        config = self.config
        waiting = sorted([*self._pending, *self._startup_waves], key=lambda candidate: candidate.time_s)
        candidates = self._remove_duplicates(waiting, self._recent_startup)
        evidence = [*self._recent_startup, *candidates]
        supports = {candidate: self._startup_support(evidence, candidate) for candidate in candidates}
        confirmed = [candidate for candidate in candidates if supports[candidate] is not None]
        if confirmed:
            self._settle_startup(confirmed, supports, events)
            waves = [candidate for candidate in confirmed if candidate.wave]
            self._consume_startup(confirmed[-1].time_s)
            if waves:
                for wave in waves:
                    self._record_wave(wave)
                self._recent_startup.clear()
                self._discard_before_wave()
                for wave in self._startup_waves:
                    self._observe_wave(wave, events)
                self._startup_waves.clear()
            if finishing and self._last_wave is None:
                self._review_startup(events, finishing=True)
            return

        first_wave = self._startup_waves[0] if self._startup_waves else None
        wave_expired = (first_wave is not None and
                        self._time - first_wave.time_s >= config.startup_wave_periods * first_wave.period_s)
        idle = self._time - waiting[-1].time_s >= config.startup_idle_s
        cutoff = self._time - config.startup_wait_s
        if not (finishing or idle or wave_expired or waiting[0].time_s <= cutoff):
            return
        pair = self._short_pair(candidates) if config.allow_short_pair else ()
        if pair:
            self._settle_startup(pair, supports, events)
            cutoff = max(cutoff, pair[-1].time_s)
        if finishing or idle:
            self._pending.clear()
            self._startup_waves.clear()
            self._recent_startup.clear()
        else:
            if wave_expired:
                cutoff = max(cutoff, first_wave.time_s)
            self._consume_startup(cutoff)

    def _consume_startup(self, through):
        self._pending = [candidate for candidate in self._pending if candidate.time_s > through]
        self._startup_waves = [wave for wave in self._startup_waves if wave.time_s > through]

    def _short_pair(self, candidates):
        minimum = self.config.pair_min_score
        anchors = [candidate for candidate in candidates if
                   candidate.wave or (candidate.strong and candidate.score >= minimum)]
        best = None
        for first, second in combinations(anchors, 2):
            interval = self._phase_time(second) - self._phase_time(first)
            waves = [candidate for candidate in (first, second) if candidate.wave]
            if not (MIN_PERIOD <= interval <= MAX_PERIOD) or (self.config.pair_requires_wave and not waves):
                continue
            if any(not self._near(interval, wave.period_s) for wave in waves):
                continue
            quality = (len(waves), -sum(abs(interval - wave.period_s) / interval for wave in waves),
                       first.score + second.score)
            if best is None or quality > best[0]:
                best = (quality, (first, second))
        return best[1] if best is not None else ()

    def _settle_startup(self, candidates, supports, events):
        for candidate in candidates:
            if candidate.wave:
                if self._publish(candidate, "waveform", candidate.period_s, events):
                    self._recent_startup.append(candidate)
            elif candidate.strong:
                if self._publish(candidate, "startup_landing", None, events):
                    self._recent_startup.append(candidate)
            elif supports[candidate] is not None:
                if self._publish(candidate, "startup_recovered", supports[candidate][1], events):
                    self._recent_startup.append(candidate)

    def _period(self):
        return median(self._wave_intervals) if self._wave_intervals else self._last_wave.period_s

    def _observe_wave(self, wave, events):
        if self._last_wave is None:
            self._startup_waves.append(wave)
            return
        if self.recover_on_loss and (self._holding or
                wave.time_s - self._last_wave.time_s >= self.config.loss_trigger_periods * self._period()):
            self._recover_loss(wave, events)
        self._publish(wave, "waveform", wave.period_s, events)
        self._record_wave(wave)
        self._discard_before_wave()
        self._holding = False

    def _record_wave(self, wave):
        if self._last_wave is not None:
            interval = wave.time_s - self._last_wave.time_s
            reference = self._period()
            if MIN_PERIOD <= interval <= MAX_PERIOD and self._near(interval, wave.period_s):
                if not self._near(interval, reference):
                    self._wave_intervals.clear()
                self._wave_intervals.append(interval)
        self._last_wave = wave

    def _discard_before_wave(self):
        self._pending = [candidate for candidate in self._pending if
                         candidate.time_s - self._last_wave.time_s >= _SPACING - _EPSILON
                         and not self._overlap(candidate, self._last_wave)]

    def _at_hold_deadline(self, time):
        period = self._period()
        wait = min(self.config.max_loss_wait_s, self.config.hold_periods * period)
        return time - self._last_wave.time_s >= wait + self.landings.config.wait_s

    def _close_hold(self, events):
        if self.recover_on_loss:
            self._recover_loss(None, events)
        self._pending.clear()
        self._wave_intervals.clear()
        self._last_wave = None
        self._holding = False

    def _recover_loss(self, right, events):
        left = self._last_wave
        period = self._period()
        if right is None:
            if not self.config.allow_unbracketed_strong or len(self._wave_intervals) < 2:
                return
            slots = range(1, int(min(self.config.hold_periods, self.config.max_loss_wait_s / period)) + 1)
        else:
            span = right.time_s - left.time_s
            beats = round(span / period)
            if beats < 2 or not self._near(span / beats, period):
                return
            period = span / beats
            slots = range(1, beats)
        for slot in slots:
            expected = left.time_s + slot * period
            candidates = [candidate for candidate in self._pending if (right is not None or candidate.strong)
                          and self._near(self._phase_time(candidate) - expected + period, period)
                          and not self._overlap(candidate, left)
                          and (right is None or not self._overlap(candidate, right))]
            if candidates:
                best = min(candidates, key=lambda candidate: (
                    not candidate.strong, abs(self._phase_time(candidate) - expected), -candidate.score))
                self._publish(best, "loss_recovered", period, events)

    def _publish(self, candidate, source, period, events):
        if self._last_event is not None and candidate.time_s - self._last_event.time_s < _SPACING - _EPSILON:
            return False
        self.count += 1
        event = CountEvent(self._span, candidate.time_s, self._time, source, self.count, period)
        self._last_event = event
        events.append(event)
        return True
