"""Streaming dual rhythm detector for the recorded right-wrist mounting."""
from collections import deque
from dataclasses import dataclass
from math import hypot, isfinite
from statistics import median

from .running_steps import MAX_GAP_SECONDS, RunningCounter


ALGORITHM_ID = "running_rhythm"


@dataclass(frozen=True)
class RhythmUpdate:
    steps: tuple
    arm_turns: tuple
    arm_cycles: int  # Newly completed cycles on this packet
    steps_per_min: float | None
    swings_per_min: float | None


class Rate:
    def __init__(self, shortest, longest):
        self.shortest, self.longest = shortest, longest
        self.last = None
        self.intervals = deque(maxlen=5)

    def add(self, event):
        interval = None if self.last is None else event - self.last
        self.last = event
        if interval is None or not self.shortest <= interval <= self.longest:
            self.intervals.clear()
            return False
        self.intervals.append(interval)
        return True

    def value(self, now):
        if self.intervals and now - self.last <= self.longest:
            return 60 / median(self.intervals)
        return None


class RunningRhythmCounter:
    def __init__(self):
        self.steps = RunningCounter()
        self.step_count = 0
        self.arm_cycle_count = 0
        self._reset()

    def _reset(self):
        self.last = self.gy = self.segment = None
        self.negative = False
        self.crossing = self.impact = self.candidate = self.pending = None
        self.step_rate = Rate(0.23, 0.58)
        self.arm_rate = Rate(0.46, 1.16)

    def _arm(self, t, gy):
        if not isfinite(gy):
            self.gy = self.crossing = self.candidate = self.pending = None
            self.negative = False
            self.arm_rate = Rate(0.46, 1.16)
            return ()
        if gy <= -65:
            self.negative = True
            self.crossing = None
        if self.negative and self.gy is not None and self.gy < 0 <= gy:
            self.crossing = None
            if t - self.last <= 0.08 + 1e-9:
                self.crossing = self.last - self.gy * (t-self.last) / (gy-self.gy)
        self.gy = gy
        if gy >= 65 and self.negative:
            self.negative = False
            if self.crossing is not None and t-self.crossing <= 0.25:
                self.pending = self.crossing
        turn = self.pending
        if turn is None or t-turn > 0.25 or self.impact is None or t-self.impact > 0.55:
            return ()
        self.pending = None
        prev = self.candidate
        if prev is not None and turn-prev < self.arm_rate.shortest:
            return ()
        self.candidate = turn
        if prev is None or turn-prev > self.arm_rate.longest:
            return ()
        if self.arm_rate.last == prev:
            return (turn,)
        return (prev, turn)

    def push(self, t, ax, ay, az, gx, gy, gz, segment=0):
        """Consume one packet in seconds, g, degrees/s. Rates expire during rest."""
        if not isfinite(t) or (segment == self.segment and self.last is not None and t <= self.last):
            now = self.last if self.last is not None else 0
            return RhythmUpdate((), (), 0, self.step_rate.value(now), self.arm_rate.value(now))
        if segment != self.segment or self.last is None or t-self.last > MAX_GAP_SECONDS + 1e-9:
            self._reset()
            self.segment = segment
        steps = self.steps.push(t, ax, ay, az, segment)
        acc = hypot(ax, ay, az)
        if isfinite(acc) and acc >= 2.6:
            self.impact = t
        turns = self._arm(t, gy)
        self.last = t
        for step in steps:
            self.step_rate.add(step)
        cycles = sum(self.arm_rate.add(turn) for turn in turns)
        self.step_count += len(steps)
        self.arm_cycle_count += cycles
        return RhythmUpdate(tuple(steps), turns, cycles, self.step_rate.value(t), self.arm_rate.value(t))
