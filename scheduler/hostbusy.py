"""Host-busy rule (plan §7.2): a pure state machine over foreign-CPU samples, one per worker."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

WINDOW = 10
MIN_SAMPLES = 5


@dataclass
class HostBusy:
    high: float = 70.0
    low: float = 50.0
    window: int = WINDOW
    min_samples: int = MIN_SAMPLES
    busy: bool = False
    samples: deque = field(default_factory=deque)

    def __post_init__(self):
        if not 0 <= self.low < self.high <= 100:
            raise ValueError(f"need 0 <= low < high <= 100, got low={self.low} high={self.high}")
        self.samples = deque(self.samples, maxlen=self.window)

    @property
    def mean(self) -> float | None:
        return sum(self.samples) / len(self.samples) if self.samples else None

    def add(self, foreign_cpu_pct: float) -> bool:
        """Record one sample. Returns True when the busy state changed.

        Enter busy: at least `min_samples` samples and mean >= high.
        Leave busy: mean <= low. In between, the previous state holds (hysteresis)."""
        self.samples.append(float(foreign_cpu_pct))
        m = self.mean
        if not self.busy and len(self.samples) >= self.min_samples and m >= self.high:
            self.busy = True
            return True
        if self.busy and m <= self.low:
            self.busy = False
            return True
        return False

    def reset(self) -> None:
        """Forget the samples (e.g. after the worker was stale); the state becomes free."""
        self.samples.clear()
        self.busy = False
