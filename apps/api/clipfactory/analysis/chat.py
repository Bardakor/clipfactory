"""Chat activity as a signal: is this 10-second bucket abnormal *for this channel*?

The same idea as the audio detector: novelty against a trailing baseline, not level.
A chat that is always busy never fires; a quiet one fires only when real people show up.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from statistics import median

BUCKET_S = 10
BASELINE_BUCKETS = 30  # five minutes
MIN_HISTORY = 6


def spike_score(history: Sequence[int], value: int) -> tuple[float, float] | None:
    """(z, baseline) of ``value`` against the buckets before it, or None while there is too little history.

    The scale is the robust spread, floored at sqrt(baseline): counts are Poisson-like, so a chat of
    two people per bucket swings by one or two on its own and must not be read as a spike.
    """
    if len(history) < MIN_HISTORY:
        return None
    baseline = median(history)
    spread = 1.4826 * median(abs(h - baseline) for h in history)
    return (value - baseline) / max(spread, math.sqrt(max(baseline, 1.0))), baseline


@dataclass(frozen=True)
class Spike:
    t0: int  # epoch second the bucket starts at
    chatters: int
    baseline: float
    z: float


class SpikeDetector:
    """One per channel. Fed every closed bucket in order."""

    def __init__(self) -> None:
        self._history: deque[int] = deque(maxlen=BASELINE_BUCKETS)
        self._above = False
        self._last_spike: int | None = None

    def push(self, t0: int, chatters: int, *, spike_z: float, min_chatters: int, refractory_s: int, armed: bool) -> Spike | None:
        """Record a bucket; return a spike on the rising edge only.

        ``armed`` is False when a spike could not become a clip (not recording, still warming up):
        the bucket still feeds the baseline, and a spike already in progress when arming is not an edge.
        """
        score = spike_score(self._history, chatters)
        self._history.append(chatters)
        was_above, self._above = self._above, bool(score and score[0] >= spike_z and chatters >= min_chatters)
        if not self._above or was_above or not armed:
            return None
        if self._last_spike is not None and t0 - self._last_spike < refractory_s:
            return None
        self._last_spike = t0
        z, baseline = score
        return Spike(t0, chatters, float(baseline), round(z, 2))
