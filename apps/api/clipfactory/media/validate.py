"""Export validation. A render is only ever marked completed after this passes."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from clipfactory.domain import Caption, ExportValidation
from clipfactory.errors import ClipFactoryError
from clipfactory.media import ffmpeg

DURATION_TOLERANCE_MS = 150
SYNC_TOLERANCE_MS = 80


def audio_offset_ms(reference: np.ndarray, rendered: np.ndarray, sample_rate: int, max_shift_ms: int = 500) -> int | None:
    """Offset of ``rendered`` relative to ``reference`` via envelope cross-correlation.

    Positive = the rendered audio is late. Returns None when either signal is
    too quiet to say anything meaningful.
    """
    hop = max(1, sample_rate // 200)  # 5 ms envelope resolution
    n = min(len(reference), len(rendered)) // hop * hop
    if n < sample_rate:
        return None
    a = np.abs(reference[:n]).reshape(-1, hop).mean(axis=1)
    b = np.abs(rendered[:n]).reshape(-1, hop).mean(axis=1)
    a, b = a - a.mean(), b - b.mean()
    if a.std() < 1e-5 or b.std() < 1e-5:
        return None
    max_shift = max_shift_ms // 5
    best_shift, best_value = 0, -np.inf
    for shift in range(-max_shift, max_shift + 1):
        if shift >= 0:
            value = float(np.dot(a[: len(a) - shift], b[shift:]))
        else:
            value = float(np.dot(a[-shift:], b[: len(b) + shift]))
        if value > best_value:
            best_shift, best_value = shift, value
    return best_shift * 5


def validate_export(
    path: Path,
    *,
    expected_ms: int,
    width: int,
    height: int,
    fps: int,
    expect_audio: bool,
    captions: list[Caption] | None = None,
    reference_cut: Path | None = None,
) -> ExportValidation:
    checks: dict[str, bool] = {}
    details: dict[str, object] = {}

    checks["file_exists"] = path.exists() and path.stat().st_size > 0
    if not checks["file_exists"]:
        return ExportValidation(ok=False, checks=checks, details=details, failures=["file_exists"])

    try:
        info = ffmpeg.probe(path)
    except ClipFactoryError as exc:
        checks["probe"] = False
        details["probe_error"] = exc.message
        return ExportValidation(ok=False, checks=checks, details=details, failures=["probe"])

    details.update(
        duration_ms=info.duration_ms, width=info.width, height=info.height, fps=info.fps,
        video_codec=info.video_codec, audio_codec=info.audio_codec, bytes=path.stat().st_size,
    )  # fmt: skip
    checks["probe"] = True
    checks["h264"] = info.video_codec == "h264"
    checks["dimensions"] = (info.width, info.height) == (width, height)
    checks["vertical"] = info.height > info.width
    checks["frame_rate"] = abs(info.fps - fps) < 0.5
    checks["duration"] = abs(info.duration_ms - expected_ms) <= DURATION_TOLERANCE_MS
    details["expected_ms"] = expected_ms
    if expect_audio:
        checks["audio_stream"] = info.has_audio
        checks["aac"] = info.audio_codec == "aac"

    frames = ffmpeg.count_frames(path)
    details["frames"] = frames
    expected_frames = round(expected_ms * fps / 1000)
    details["expected_frames"] = expected_frames
    checks["frames"] = frames > 0 and abs(frames - expected_frames) <= max(2, fps // 10)

    try:
        ffmpeg.decode_check(path)
        checks["decodable"] = True
    except ClipFactoryError as exc:
        checks["decodable"] = False
        details["decode_error"] = str(exc.details.get("stderr", exc.message))[-300:]

    if captions is not None:
        ordered = all(c.endMs >= c.startMs for c in captions)
        in_range = all(0 <= c.startMs and c.endMs <= expected_ms + DURATION_TOLERANCE_MS for c in captions)
        checks["captions_in_range"] = ordered and in_range
        details["caption_tokens"] = len(captions)

    if expect_audio and reference_cut is not None and info.has_audio:
        ref = ffmpeg.read_audio_mono(reference_cut)
        out = ffmpeg.read_audio_mono(path)
        offset = audio_offset_ms(ref, out, 8000)
        details["audio_offset_ms"] = offset
        if offset is not None:  # silent clips cannot be measured; they are not failed for it
            checks["av_sync"] = abs(offset) <= SYNC_TOLERANCE_MS

    failures = [name for name, passed in checks.items() if not passed]
    return ExportValidation(ok=not failures, checks=checks, details=details, failures=failures)
