"""Vertical layout resolution: where each piece of the 16:9 source goes in the 9:16 frame.

Gaming footage is not interview footage — a centre crop would routinely hide
the action. The automatic choice therefore keeps the whole gameplay visible,
and tighter framings are only used when asked for or when the evidence for
where the action is concentrated is strong.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from clipfactory.domain import LayoutSegment, Panel, Rect, ResolvedLayout
from clipfactory.media import ffmpeg
from clipfactory.timeutil import clamp

# Platform UI overlays (TikTok / Reels): keep captions out of these bands.
SAFE_TOP = 0.11
SAFE_BOTTOM = 0.22
SAFE_SIDE = 0.07
CROP_ASPECT = 1.0  # width / height of the "smart crop" window
SALIENCY_MIN_CONFIDENCE = 0.55
MIN_DYNAMIC_SEGMENT_MS = 4000


def detect_facecam(src: Path, duration_ms: int, work_dir: Path, samples: int = 16) -> dict:
    """Look for a streamer facecam: a face that stays in the same corner region across the video.

    Returns {"found": False} unless the same region contains a face in most
    samples — in-game characters wander, an overlay does not.
    """
    cascade = cv2.CascadeClassifier(str(Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"))
    work_dir.mkdir(parents=True, exist_ok=True)
    boxes: list[tuple[float, float, float, float]] = []
    taken = 0
    for i in range(samples):
        at = int(duration_ms * (i + 0.5) / samples)
        frame_path = work_dir / f"facecam_{i}.jpg"
        try:
            ffmpeg.extract_frame(src, at, frame_path, width=960)
        except Exception:  # noqa: BLE001 - a missing sample only lowers the evidence
            continue
        image = cv2.imread(str(frame_path), cv2.IMREAD_GRAYSCALE)
        frame_path.unlink(missing_ok=True)
        if image is None:
            continue
        taken += 1
        h, w = image.shape
        for x, y, fw, fh in cascade.detectMultiScale(image, scaleFactor=1.1, minNeighbors=6, minSize=(w // 24, w // 24)):
            cx, cy = (x + fw / 2) / w, (y + fh / 2) / h
            in_corner = (cx < 0.3 or cx > 0.7) and (cy < 0.38 or cy > 0.62)
            if in_corner and fw / w < 0.22:
                boxes.append((x / w, y / h, fw / w, fh / h))
    if taken < 4 or not boxes:
        return {"found": False, "samples": taken}
    centres = np.array([(b[0] + b[2] / 2, b[1] + b[3] / 2) for b in boxes])
    median = np.median(centres, axis=0)
    near = [b for b, c in zip(boxes, centres, strict=True) if np.abs(c - median).max() < 0.06]
    if len(near) < 0.6 * taken:
        return {"found": False, "samples": taken, "hits": len(near)}
    arr = np.array(near)
    fx, fy, fw, fh = np.median(arr, axis=0)
    # Expand the face box to a plausible webcam frame (head and shoulders, 4:3-ish).
    w = clamp(fw * 3.2, 0.12, 0.4)
    h = clamp(fh * 2.6, 0.14, 0.45)
    x = clamp(fx + fw / 2 - w / 2, 0, 1 - w)
    y = clamp(fy + fh / 2 - h * 0.42, 0, 1 - h)
    return {"found": True, "samples": taken, "hits": len(near), "rect": {"x": round(x, 4), "y": round(y, 4), "w": round(w, 4), "h": round(h, 4)}}


def saliency_profile(cut: Path) -> tuple[np.ndarray, np.ndarray]:
    """Per half-second: horizontal centre of motion (0–1) and how concentrated it is (0–1)."""
    centres: list[float] = []
    confidences: list[float] = []
    previous: np.ndarray | None = None
    for frame in ffmpeg.scan_gray_frames(cut, fps=2.0, size=(160, 90)):
        current = frame.astype(np.int16)
        if previous is not None:
            columns = np.abs(current - previous).mean(axis=0)
            total = float(columns.sum())
            if total < 40:  # nearly static: no opinion
                centres.append(0.5)
                confidences.append(0.0)
            else:
                xs = (np.arange(len(columns)) + 0.5) / len(columns)
                centre = float((columns * xs).sum() / total)
                spread = float(np.sqrt((columns * (xs - centre) ** 2).sum() / total))
                centres.append(centre)
                confidences.append(clamp(1 - spread / 0.29, 0, 1))  # 0.29 = spread of uniform motion
        previous = current
    return np.asarray(centres), np.asarray(confidences)


def crop_centre(centres: np.ndarray, confidences: np.ndarray) -> tuple[float, float]:
    """One stable crop position for a span, with the confidence that it is the right one."""
    if len(centres) == 0 or confidences.sum() <= 0:
        return 0.5, 0.0
    centre = float(np.average(centres, weights=confidences + 1e-6))
    stability = clamp(1 - float(np.sqrt(np.average((centres - centre) ** 2, weights=confidences + 1e-6))) / 0.2, 0, 1)
    return centre, float(confidences.mean()) * stability


def _fit_panel(src: Rect, src_w: int, src_h: int, out_w: int, max_h: int, top: int) -> Panel:
    aspect = (src.w * src_w) / (src.h * src_h)
    w, h = out_w, int(round(out_w / aspect))
    if h > max_h:
        h, w = max_h, int(round(max_h * aspect))
    return Panel(src=src, x=(out_w - w) // 2, y=top, w=w - w % 2, h=h - h % 2)


def _full(src_w: int, src_h: int, out_w: int, out_h: int) -> tuple[list[Panel], int]:
    full = Rect(x=0, y=0, w=1, h=1)
    if src_w / src_h <= 0.62:  # already vertical: fill the frame
        return [_fit_panel(full, src_w, src_h, out_w, out_h, 0)], int(out_h * 0.7)
    panel = _fit_panel(full, src_w, src_h, out_w, out_h, 0)
    panel.y = max(int(out_h * SAFE_TOP), (out_h - panel.h) // 2 - int(out_h * 0.06))
    return [panel], _caption_below(panel, out_h)


def _crop(centre: float, src_w: int, src_h: int, out_w: int, out_h: int) -> tuple[list[Panel], int]:
    w = clamp(CROP_ASPECT * src_h / src_w, 0.2, 1.0)
    rect = Rect(x=clamp(centre - w / 2, 0, 1 - w), y=0, w=w, h=1)
    panel = _fit_panel(rect, src_w, src_h, out_w, int(out_h * 0.62), int(out_h * SAFE_TOP))
    return [panel], _caption_below(panel, out_h)


def _caption_below(panel: Panel, out_h: int) -> int:
    safe_bottom = int(out_h * (1 - SAFE_BOTTOM))
    return min(panel.y + panel.h + int(out_h * 0.075), safe_bottom - int(out_h * 0.05))


def _split(facecam: Rect, src_w: int, src_h: int, out_w: int, out_h: int) -> tuple[list[Panel], int]:
    cam = _fit_panel(facecam, src_w, src_h, out_w, int(out_h * 0.3), int(out_h * SAFE_TOP))
    cam.radius = 0
    game = _fit_panel(Rect(x=0, y=0, w=1, h=1), src_w, src_h, out_w, int(out_h * 0.45), cam.y + cam.h + 12)
    return [cam, game], _caption_below(game, out_h)


def resolve_layout(
    requested: str,
    *,
    src_w: int,
    src_h: int,
    out_w: int,
    out_h: int,
    duration_ms: int,
    facecam: dict | None = None,
    manual: Rect | None = None,
    saliency: tuple[np.ndarray, np.ndarray] | None = None,
    cuts_ms: list[int] | None = None,
) -> ResolvedLayout:
    """Turn a layout request into concrete panels, falling back to the full-gameplay
    layout whenever the evidence needed for a tighter framing is weak or missing."""
    caption_w = int(out_w * (1 - 2 * SAFE_SIDE))
    cam_rect = Rect.model_validate(facecam["rect"]) if facecam and facecam.get("found") else None

    def result(used: str, reason: str, segments: list[LayoutSegment], caption_y: int) -> ResolvedLayout:
        return ResolvedLayout(requested=requested, used=used, reason=reason, segments=segments,  # type: ignore[arg-type]
                              caption_y=caption_y, caption_max_width=caption_w)  # fmt: skip

    def whole(panels: list[Panel]) -> list[LayoutSegment]:
        return [LayoutSegment(from_ms=0, to_ms=duration_ms, panels=panels)]

    full_panels, full_caption = _full(src_w, src_h, out_w, out_h)

    if requested == "manual" and manual is not None:
        panel = _fit_panel(manual, src_w, src_h, out_w, int(out_h * 0.66), int(out_h * SAFE_TOP))
        return result("manual", "manual crop", whole([panel]), _caption_below(panel, out_h))

    if requested == "split" or (requested == "auto" and cam_rect is not None):
        if cam_rect is not None:
            panels, caption_y = _split(cam_rect, src_w, src_h, out_w, out_h)
            return result("split", "facecam detected reliably", whole(panels), caption_y)
        if requested == "split":
            return result("full", "no reliable facecam found — full gameplay used instead", whole(full_panels), full_caption)

    if requested == "crop":
        centre, confidence = crop_centre(*saliency) if saliency else (0.5, 0.0)
        if confidence < SALIENCY_MIN_CONFIDENCE:
            centre = 0.5
            reason = "action position uncertain — crop kept centred"
        else:
            reason = f"crop centred on the action (confidence {confidence:.2f})"
        panels, caption_y = _crop(centre, src_w, src_h, out_w, out_h)
        return result("crop", reason, whole(panels), caption_y)

    if requested == "dynamic" and saliency is not None:
        centres, confidences = saliency
        bounds = [0]
        for cut in sorted(cuts_ms or []):
            if cut - bounds[-1] >= MIN_DYNAMIC_SEGMENT_MS and duration_ms - cut >= MIN_DYNAMIC_SEGMENT_MS:
                bounds.append(cut)
        bounds.append(duration_ms)
        segments: list[LayoutSegment] = []
        crop_caption = full_caption
        for a, b in zip(bounds[:-1], bounds[1:], strict=True):
            centre, confidence = crop_centre(centres[a // 500 : b // 500], confidences[a // 500 : b // 500])
            if confidence >= SALIENCY_MIN_CONFIDENCE:
                panels, crop_caption = _crop(centre, src_w, src_h, out_w, out_h)
            else:
                panels = full_panels
            if segments and segments[-1].panels == panels:
                segments[-1].to_ms = b
            else:
                segments.append(LayoutSegment(from_ms=a, to_ms=b, panels=panels))
        if len(segments) > 1 or segments[0].panels != full_panels:
            # One caption line for the whole clip, below the tallest panel, so text never jumps.
            return result("dynamic", f"{len(segments)} framing segment(s) on shot changes", segments, max(full_caption, crop_caption))
        return result("full", "no shot had a clearly concentrated action — full gameplay kept", whole(full_panels), full_caption)

    return result("full", "full gameplay preserved", whole(full_panels), full_caption)
