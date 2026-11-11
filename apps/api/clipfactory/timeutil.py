"""Timestamp helpers. The canonical unit everywhere is integer milliseconds."""

from __future__ import annotations

from datetime import UTC, datetime


def now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def s_to_ms(seconds: float) -> int:
    return int(round(seconds * 1000))


def ms_to_s(ms: int) -> float:
    return ms / 1000.0


def ms_to_frame(ms: int, fps: float) -> int:
    """Nearest frame index for a timestamp."""
    return int(round(ms * fps / 1000.0))


def frame_to_ms(frame: int, fps: float) -> int:
    return int(round(frame * 1000.0 / fps))


def frames_for(start_ms: int, end_ms: int, fps: float) -> int:
    """Frame count of [start, end) at ``fps`` — always at least one frame."""
    return max(1, ms_to_frame(end_ms - start_ms, fps))


def snap_to_frame(ms: int, fps: float) -> int:
    return frame_to_ms(ms_to_frame(ms, fps), fps)


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def clamp_ms(ms: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, ms))


def overlap_ms(a0: int, a1: int, b0: int, b1: int) -> int:
    return max(0, min(a1, b1) - max(a0, b0))


def iou(a0: int, a1: int, b0: int, b1: int) -> float:
    inter = overlap_ms(a0, a1, b0, b1)
    union = (a1 - a0) + (b1 - b0) - inter
    return inter / union if union > 0 else 0.0


def ffmpeg_ts(ms: int) -> str:
    """Seconds with millisecond precision, as FFmpeg expects for -ss / -t."""
    return f"{ms / 1000.0:.3f}"


def srt_ts(ms: int) -> str:
    ms = max(0, ms)
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, milli = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{milli:03d}"


def clock(ms: int) -> str:
    s = max(0, ms) // 1000
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"
