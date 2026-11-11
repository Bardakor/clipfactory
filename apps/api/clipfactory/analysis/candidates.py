"""Signal fusion and candidate discovery.

Builds a one-value-per-second interest curve from independent audio, visual and
speech signals, picks its peaks as seeds, and scores any window against it.
Loudness on its own contributes little: what scores is *contrast* and a spoken
reaction right after it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from clipfactory.analysis.audio import HOP_MS, AudioFeatures, SpeechStats
from clipfactory.analysis.visual import SCAN_FPS, VisualFeatures
from clipfactory.domain import Segment
from clipfactory.timeutil import clamp, iou


@dataclass
class Signals:
    """All first-pass evidence resampled to one value per second."""

    duration_ms: int
    interest: np.ndarray
    energy: np.ndarray
    flux: np.ndarray
    motion: np.ndarray
    reaction: np.ndarray
    speech_active: np.ndarray
    speech_density: np.ndarray
    silent: np.ndarray

    @property
    def seconds(self) -> int:
        return len(self.interest)


@dataclass
class CandidateWindow:
    start_ms: int
    end_ms: int
    origins: list[str]
    title: str = ""
    event_type: str = "unknown"
    structure: dict = field(default_factory=dict)
    confidence: float | None = None
    seed_ms: int | None = None

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


def _per_second(values: np.ndarray, per_s: float, n: int, reducer=np.max) -> np.ndarray:  # noqa: ANN001
    out = np.zeros(n, dtype=np.float32)
    step = int(round(per_s))
    usable = min(len(values) // step, n)
    if usable:
        out[:usable] = reducer(values[: usable * step].reshape(usable, step), axis=1)
    return out


def _squash(z: np.ndarray) -> np.ndarray:
    """Novelty z-score → 0–1, ignoring anything below baseline."""
    return np.tanh(np.clip(z, 0, None) / 3.0)


def build_signals(duration_ms: int, audio: AudioFeatures | None, visual: VisualFeatures | None, speech: SpeechStats) -> Signals:
    n = max(1, duration_ms // 1000 + 1)
    zeros = np.zeros(n, dtype=np.float32)
    if audio is not None and len(audio.rms_db):
        energy = _squash(_per_second(audio.energy_z, 1000 / HOP_MS, n))
        flux = _squash(_per_second(audio.flux_z, 1000 / HOP_MS, n))
        silent = _per_second(audio.silent.astype(np.float32), 1000 / HOP_MS, n, np.mean) > 0.8
    else:
        energy, flux, silent = zeros, zeros, np.zeros(n, dtype=bool)
    motion = _squash(_per_second(visual.motion_z, SCAN_FPS, n)) if visual is not None and len(visual.motion) else zeros

    active = np.zeros(n, dtype=bool)
    active[: min(n, len(speech.active))] = speech.active[:n]
    density = np.zeros(n, dtype=np.float32)
    density[: min(n, len(speech.density))] = speech.density[:n]
    typical = float(np.median(density[density > 0])) if (density > 0).any() else 1.0
    density_norm = np.clip(density / (2.0 * typical), 0, 1)

    # A reaction: something stands out, and someone speaks within the next three seconds.
    spike = np.maximum.reduce([energy, flux, motion])
    speaks_soon = np.convolve(active.astype(np.float32), np.ones(4), mode="full")[3 : n + 3] > 0
    reaction = spike * speaks_soon[:n] if len(speaks_soon) >= n else spike * 0

    raw = 0.28 * energy + 0.12 * flux + 0.20 * motion + 0.15 * density_norm + 0.25 * reaction
    interest = np.convolve(raw, np.ones(5) / 5, mode="same").astype(np.float32)
    return Signals(duration_ms, interest, energy, flux, motion, reaction.astype(np.float32), active, density, silent & ~active)


def find_seeds(signals: Signals, limit: int, min_distance_s: int = 18) -> list[int]:
    """Second indices of the strongest, well-separated interest peaks."""
    curve = signals.interest
    if len(curve) < 3 or curve.max() <= 0:
        return []
    floor = max(float(np.percentile(curve, 70)), 0.04)
    order = np.argsort(curve)[::-1]
    picked: list[int] = []
    for idx in order:
        if curve[idx] < floor or len(picked) >= limit:
            break
        if all(abs(int(idx) - p) >= min_distance_s for p in picked):
            picked.append(int(idx))
    return sorted(picked)


def window_around(seed_s: int, signals: Signals, min_len_ms: int, max_len_ms: int) -> tuple[int, int]:
    """A setup-then-payoff window: more room before the peak than after it."""
    target = int(clamp(26_000, min_len_ms, max_len_ms))
    start = seed_s * 1000 - int(target * 0.55)
    end = start + target
    if start < 0:
        start, end = 0, min(signals.duration_ms, target)
    if end > signals.duration_ms:
        end = signals.duration_ms
        start = max(0, end - target)
    return start, end


def signal_candidates(signals: Signals, count: int, min_len_ms: int, max_len_ms: int) -> list[CandidateWindow]:
    out = []
    for seed in find_seeds(signals, count):
        start, end = window_around(seed, signals, min_len_ms, max_len_ms)
        if end - start >= min(min_len_ms, signals.duration_ms):
            out.append(CandidateWindow(start, end, ["signals"], seed_ms=seed * 1000))
    return out


def window_features(signals: Signals, start_ms: int, end_ms: int) -> dict[str, float]:
    """Observable, deterministic properties of a window — the evidence behind its heuristic score."""
    a, b = start_ms // 1000, max(start_ms // 1000 + 1, min(signals.seconds, -(-end_ms // 1000)))
    interest = signals.interest[a:b]
    if len(interest) == 0:
        return {"peak": 0, "mean": 0, "speech_coverage": 0, "dead_time": 1, "hook": 0, "payoff_position": 0,
                "motion": 0, "reaction": 0, "duration_s": 0}  # fmt: skip
    scale = max(float(np.percentile(signals.interest, 99)), 1e-3)
    peak_at = int(np.argmax(interest))
    head = interest[:3]
    speech_head = signals.speech_active[a : a + 2].any()
    return {
        "peak": round(clamp(float(interest.max()) / scale, 0, 1), 3),
        "mean": round(clamp(float(interest.mean()) / scale, 0, 1), 3),
        "speech_coverage": round(float(signals.speech_active[a:b].mean()), 3),
        "dead_time": round(float(signals.silent[a:b].mean()), 3),
        "hook": round(clamp(0.6 * float(head.mean()) / scale + (0.4 if speech_head else 0.0), 0, 1), 3),
        "payoff_position": round(peak_at / max(1, len(interest) - 1), 3),
        "motion": round(float(signals.motion[a:b].mean()), 3),
        "reaction": round(float(signals.reaction[a:b].max()), 3),
        "duration_s": round((end_ms - start_ms) / 1000, 1),
    }


def length_fit(duration_s: float) -> float:
    """1.0 in the 20–50 s sweet spot, tapering towards very short or very long clips."""
    if 20 <= duration_s <= 50:
        return 1.0
    if duration_s < 20:
        return clamp(0.5 + duration_s / 40, 0.5, 1.0)
    return clamp(1.0 - (duration_s - 50) / 100, 0.55, 1.0)


def heuristic_score(features: dict[str, float]) -> float:
    """0–1 preliminary score from signals alone. Not an editorial judgement."""
    payoff = 1.0 - abs(features["payoff_position"] - 0.65) / 0.65  # best when the peak lands ~2/3 in
    score = (
        0.32 * features["peak"]
        + 0.18 * features["mean"]
        + 0.14 * features["speech_coverage"]
        + 0.10 * features["hook"]
        + 0.10 * features["reaction"]
        + 0.08 * clamp(payoff, 0, 1)
        + 0.08 * length_fit(features["duration_s"])
        - 0.25 * features["dead_time"]
    )
    return round(clamp(score, 0, 1), 4)


def merge_candidates(windows: list[CandidateWindow], threshold: float = 0.55) -> list[CandidateWindow]:
    """Collapse windows describing the same moment. Model-proposed boundaries win over signal ones."""

    def priority(w: CandidateWindow) -> tuple[int, float]:
        return (1 if "transcript_llm" in w.origins else 0, w.confidence or 0.0)

    merged: list[CandidateWindow] = []
    for window in sorted(windows, key=priority, reverse=True):
        match = next((m for m in merged if iou(m.start_ms, m.end_ms, window.start_ms, window.end_ms) >= threshold), None)
        if match is None:
            merged.append(window)
        else:
            match.origins = sorted(set(match.origins) | set(window.origins))
            match.seed_ms = match.seed_ms if match.seed_ms is not None else window.seed_ms
    return sorted(merged, key=lambda w: w.start_ms)


def transcript_excerpt(segments: list[Segment], start_ms: int, end_ms: int, *, relative_to: int | None = None) -> str:
    """Timestamped transcript lines for a window, as given to language models."""
    base = relative_to or 0
    lines = [
        f"[{(s.start_ms - base) / 1000:.1f}s] {s.text}"
        for s in segments
        if s.end_ms > start_ms and s.start_ms < end_ms
    ]
    return "\n".join(lines) or "(no speech)"
