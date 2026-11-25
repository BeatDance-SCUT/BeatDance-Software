"""Causal wrist-IMU running step counter. Input acceleration is in g."""
from collections import deque
from math import exp, hypot, isfinite


MAX_GAP_SECONDS = 0.20


class RunningCounter:
    def __init__(self):
        self.segment = None
        self.last = None
        self.filtered = 0.0
        self.peak = None
        self.ready = False
        self.recent = deque(maxlen=3)
        self.running = False

    def push(self, t, ax, ay, az, segment=0):
        """Return impact onset timestamps accepted on this packet (possibly retroactive)."""
        value = hypot(ax, ay, az)
        if not isfinite(t) or not isfinite(value):
            return []
        if self.segment == segment and self.last is not None and t <= self.last:
            return []
        if self.segment != segment or self.last is None or t - self.last > MAX_GAP_SECONDS + 1e-9:
            self.__init__()
            self.segment = segment
            self.last = t
            self.filtered = value
            self.ready = value <= 1.8
            return []
        dt = t - self.last
        self.last = t
        self.filtered += (1 - exp(-dt / 0.025)) * (value - self.filtered)
        if self.filtered <= 1.8:
            self.ready = True
        if self.ready and self.filtered >= 2.6 and self.peak is None:
            self.peak = self.filtered
            self.onset = t
            self.ready = False
        if self.peak is None:
            return []
        if self.filtered >= self.peak:
            self.peak = self.filtered
            return []
        if self.filtered > self.peak - 0.45:
            return []
        onset = self.onset
        self.peak = None
        if self.recent:
            interval = onset - self.recent[-1]
            if interval < 0.23:
                return []
            if interval > 0.58:
                self.running = False
                self.recent.clear()
        self.recent.append(onset)
        if self.running:
            return [onset]
        if len(self.recent) == 3:
            first, middle, last = self.recent
            if abs((last - middle) - (middle - first)) <= 0.13:
                self.running = True
                return list(self.recent)
        return []
