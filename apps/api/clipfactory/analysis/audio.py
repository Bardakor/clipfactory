"""Deterministic acoustic analysis, independent of the transcript.

The audio is read in one-minute blocks, so memory use does not grow with source
duration. Everything is expressed as *novelty* — how much a moment stands out
from the preceding half minute — because sustained loudness (a long gunfight) is
not, by itself, interesting.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import librosa
import numpy as np
import soundfile as sf

from clipfactory.domain import Segment
from clipfactory.settings_store import AudioDetectorSettings

HOP_MS = 100
_BLOCK_S = 60
_BASELINE_S = 30


@dataclass
class AudioEventData:
    kind: str
    start_ms: int
    end_ms: int
    peak_ms: int
    score: float
    meta: dict


@dataclass
class AudioFeatures:
    rms_db: np.ndarray  # loudness per 100 ms
    energy_z: np.ndarray  # loudness novelty vs. the preceding 30 s (robust z-score)
    flux_z: np.ndarray  # spectral-flux / onset novelty (robust z-score)
    silent: np.ndarray  # bool per 100 ms

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(path, rms_db=self.rms_db, energy_z=self.energy_z, flux_z=self.flux_z, silent=self.silent)

    @classmethod
    def load(cls, path: Path) -> AudioFeatures:
        d = np.load(path)
        return cls(rms_db=d["rms_db"], energy_z=d["energy_z"], flux_z=d["flux_z"], silent=d["silent"])


def robust_z(x: np.ndarray) -> np.ndarray:
    med = np.median(x)
    mad = np.median(np.abs(x - med)) * 1.4826
    return (x - med) / (mad if mad > 1e-6 else (x.std() or 1.0))


def trailing_median(x: np.ndarray, window: int) -> np.ndarray:
    """Median of the ``window`` samples *before* each point (causal baseline)."""
    if len(x) == 0:
        return x
    step = max(1, window // 10)  # evaluated every few hops and interpolated: plenty for a baseline
    idx = np.arange(0, len(x), step)
    med = np.array([np.median(x[max(0, i - window) : i + 1]) for i in idx])
    return np.interp(np.arange(len(x)), idx, med)


def extract_features(wav: Path, cfg: AudioDetectorSettings, should_stop=lambda: None, on_progress=lambda f: None) -> AudioFeatures:  # noqa: ANN001
    info = sf.info(str(wav))
    sr = info.samplerate
    hop = sr * HOP_MS // 1000
    rms_parts: list[np.ndarray] = []
    flux_parts: list[np.ndarray] = []
    total_blocks = max(1, info.frames // (sr * _BLOCK_S) + 1)
    for i, block in enumerate(sf.blocks(str(wav), blocksize=sr * _BLOCK_S, dtype="float32", always_2d=False)):
        should_stop()
        n = len(block) // hop
        if n == 0:
            continue
        frames = block[: n * hop].reshape(n, hop)
        rms_parts.append(np.sqrt(np.mean(np.square(frames), axis=1) + 1e-12))
        onset = librosa.onset.onset_strength(y=block[: n * hop], sr=sr, hop_length=hop // 4, n_fft=1024)
        usable = (len(onset) // 4) * 4
        pooled = onset[:usable].reshape(-1, 4).max(axis=1) if usable else np.zeros(0)
        flux_parts.append(np.pad(pooled, (0, max(0, n - len(pooled))))[:n])
        on_progress(min(1.0, (i + 1) / total_blocks))
    if not rms_parts:
        empty = np.zeros(0, dtype=np.float32)
        return AudioFeatures(empty, empty, empty, empty.astype(bool))
    rms_db = (20 * np.log10(np.concatenate(rms_parts))).astype(np.float32)
    flux = np.concatenate(flux_parts).astype(np.float32)
    baseline = _BASELINE_S * 1000 // HOP_MS
    smooth = np.convolve(rms_db, np.ones(3) / 3, mode="same")
    energy_z = robust_z(smooth - trailing_median(smooth, baseline)).astype(np.float32)
    flux_z = robust_z(flux - trailing_median(flux, baseline)).astype(np.float32)
    return AudioFeatures(rms_db=rms_db, energy_z=energy_z, flux_z=flux_z, silent=rms_db < cfg.silence_db)


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """[start, end) index runs where ``mask`` is true."""
    if not mask.any():
        return []
    padded = np.concatenate([[False], mask, [False]])
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    return list(zip(edges[::2].tolist(), edges[1::2].tolist(), strict=True))


def _confidence(z: float, threshold: float) -> float:
    """Squash a novelty z-score into 0–1; exactly at threshold → 0.5."""
    return float(1.0 / (1.0 + np.exp(-(z - threshold))))


def silence_intervals(features: AudioFeatures, cfg: AudioDetectorSettings) -> list[tuple[int, int]]:
    min_hops = max(1, cfg.min_silence_ms // HOP_MS)
    return [(a * HOP_MS, b * HOP_MS) for a, b in _runs(features.silent) if b - a >= min_hops]


def detect_events(features: AudioFeatures, cfg: AudioDetectorSettings) -> list[AudioEventData]:
    """Threshold the novelty curves and group adjacent detections into single events."""
    events: list[AudioEventData] = []
    gap = max(1, cfg.group_gap_ms // HOP_MS)

    def grouped(mask: np.ndarray) -> list[tuple[int, int]]:
        merged: list[tuple[int, int]] = []
        for a, b in _runs(mask):
            if merged and a - merged[-1][1] <= gap:
                merged[-1] = (merged[-1][0], b)
            else:
                merged.append((a, b))
        return merged

    for kind, curve in (("energy_peak", features.energy_z), ("transient_burst", features.flux_z)):
        for a, b in grouped(curve > cfg.peak_z):
            peak = a + int(np.argmax(curve[a:b]))
            events.append(AudioEventData(kind, a * HOP_MS, b * HOP_MS, peak * HOP_MS,
                                         _confidence(float(curve[peak]), cfg.peak_z), {"z": round(float(curve[peak]), 2)}))  # fmt: skip

    # Sudden loudness change: a jump of 12 dB or more within 300 ms.
    if len(features.rms_db) > 3:
        jump = np.zeros_like(features.rms_db)
        jump[3:] = features.rms_db[3:] - features.rms_db[:-3]
        for a, b in grouped(jump > 12.0):
            peak = a + int(np.argmax(jump[a:b]))
            events.append(AudioEventData("sudden_loudness", a * HOP_MS, b * HOP_MS, peak * HOP_MS,
                                         _confidence(float(jump[peak]) / 6.0, 2.0), {"jump_db": round(float(jump[peak]), 1)}))  # fmt: skip

    for a_ms, b_ms in silence_intervals(features, cfg):
        if b_ms - a_ms >= 2000:  # only long pauses are stored as events; short ones stay in the curve
            events.append(AudioEventData("silence", a_ms, b_ms, (a_ms + b_ms) // 2, 1.0, {}))
    events.sort(key=lambda e: e.start_ms)
    return events


@dataclass
class SpeechStats:
    active: np.ndarray  # bool per second
    density: np.ndarray  # words per second, smoothed over 5 s
    turns: np.ndarray  # segment starts per 10 s — a proxy for conversational tempo


def speech_stats(segments: list[Segment], duration_ms: int) -> SpeechStats:
    n = max(1, duration_ms // 1000 + 1)
    active = np.zeros(n, dtype=bool)
    words = np.zeros(n, dtype=np.float32)
    starts = np.zeros(n, dtype=np.float32)
    for seg in segments:
        a, b = min(n - 1, seg.start_ms // 1000), min(n - 1, seg.end_ms // 1000)
        active[a : b + 1] = True
        starts[a] += 1
        if seg.words:
            for w in seg.words:
                words[min(n - 1, w.s // 1000)] += 1
        else:  # no word timing: spread the segment's word count evenly over its span
            count = len(seg.text.split())
            words[a : b + 1] += count / max(1, b - a + 1)
    return SpeechStats(
        active=active,
        density=np.convolve(words, np.ones(5) / 5, mode="same"),
        turns=np.convolve(starts, np.ones(10), mode="same"),
    )
