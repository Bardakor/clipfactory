"""Cheap first-pass visual scan: motion intensity, scene cuts, static stretches.

Runs on 160×90 grayscale frames at 2 fps streamed from FFmpeg. Its only job is
to tell the expensive multimodal stage where *not* to look.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from clipfactory.analysis.audio import _runs, robust_z, trailing_median
from clipfactory.media import ffmpeg

SCAN_FPS = 2.0
STEP_MS = int(1000 / SCAN_FPS)


@dataclass
class VisualFeatures:
    motion: np.ndarray  # mean absolute frame difference (0–1) per scan step
    motion_z: np.ndarray  # novelty vs. the preceding 30 s
    cuts_ms: np.ndarray  # timestamps of visual discontinuities

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(path, motion=self.motion, motion_z=self.motion_z, cuts_ms=self.cuts_ms)

    @classmethod
    def load(cls, path: Path) -> VisualFeatures:
        d = np.load(path)
        return cls(motion=d["motion"], motion_z=d["motion_z"], cuts_ms=d["cuts_ms"])


def analyse_motion(diffs: np.ndarray) -> VisualFeatures:
    """Turn raw frame-to-frame differences into motion novelty and scene cuts."""
    if len(diffs) == 0:
        return VisualFeatures(diffs, diffs, np.zeros(0, dtype=np.int64))
    # A cut is a difference far above what the last few seconds looked like.
    local = trailing_median(diffs, 10)
    is_cut = (diffs > 0.16) & (diffs > 4.0 * np.maximum(local, 0.01))
    # Cuts are discontinuities, not motion: replace them before measuring motion.
    motion = np.where(is_cut, local, diffs).astype(np.float32)
    smooth = np.convolve(motion, np.ones(3) / 3, mode="same")
    motion_z = robust_z(smooth - trailing_median(smooth, int(30 * SCAN_FPS))).astype(np.float32)
    return VisualFeatures(motion=motion, motion_z=motion_z, cuts_ms=np.flatnonzero(is_cut) * STEP_MS)


def scan(src: Path, duration_ms: int, should_stop=lambda: None, on_progress=lambda f: None) -> VisualFeatures:  # noqa: ANN001
    expected = max(1, int(duration_ms / 1000 * SCAN_FPS))
    diffs: list[float] = []
    previous: np.ndarray | None = None
    for i, frame in enumerate(ffmpeg.scan_gray_frames(src, fps=SCAN_FPS, should_stop=should_stop)):
        current = frame.astype(np.int16)
        diffs.append(0.0 if previous is None else float(np.abs(current - previous).mean()) / 255.0)
        previous = current
        if i % 40 == 0:
            on_progress(min(1.0, i / expected))
    return analyse_motion(np.asarray(diffs, dtype=np.float32))


def motion_spans(features: VisualFeatures) -> list[tuple[str, int, int, float]]:
    """(kind, start_ms, end_ms, confidence) for sustained high motion and long static stretches."""
    spans: list[tuple[str, int, int, float]] = []
    for a, b in _runs(features.motion_z > 1.5):
        if b - a >= int(2 * SCAN_FPS):
            peak = float(features.motion_z[a:b].max())
            spans.append(("high_motion", a * STEP_MS, b * STEP_MS, float(1 / (1 + np.exp(-(peak - 1.5))))))
    for a, b in _runs(features.motion < 0.004):
        if b - a >= int(20 * SCAN_FPS):
            spans.append(("static", a * STEP_MS, b * STEP_MS, 1.0))
    return spans
