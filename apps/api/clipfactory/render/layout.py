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
SPLIT_CAM_MIN, SPLIT_CAM_MAX = 0.26, 0.38  # share of the output height given to the streamer
SPLIT_CAPTION_OFFSET = 0.08  # captions sit on the game, this far from the seam between the two panels


# ── facecam ─────────────────────────────────────────────────────────────────
FACECAM_VERSION = 2  # bump to re-detect on sources analysed by an older detector
_YUNET = Path(__file__).resolve().parent.parent / "assets" / "face_detection_yunet_2023mar.onnx"
FACE_SCORE = 0.6
MAX_FACE_WIDTH = 0.22  # wider than this is a full-screen camera, not an overlay
MIN_FACE_HIT_RATIO = 0.5
EDGE_GRADIENT = 40.0  # Sobel response that counts as an edge in one frame
MIN_LINE_PERSISTENCE = 0.35  # share of frames in which a candidate border line is an edge
MIN_WINDOW_SCORE = 0.55  # weakest of the four borders, each measured along its whole length
WINDOW_INSET = 0.02  # trimmed from each side of a measured window, so its decorative frame is not shown

Box = tuple[float, float, float, float]  # x, y, w, h — normalised to the frame


def stable_face(per_frame: list[list[Box]]) -> tuple[Box | None, int]:
    """The face that sits at the same place in the most frames, and in how many frames it does.
    In-game characters wander; a webcam overlay does not."""
    flat = [(i, box) for i, boxes in enumerate(per_frame) for box in boxes if box[2] < MAX_FACE_WIDTH]
    best: dict[int, Box] = {}
    for _, (x, y, w, h) in flat:
        near: dict[int, Box] = {}
        for j, other in flat:
            ox, oy, ow, oh = other
            if abs(ox + ow / 2 - x - w / 2) < 0.05 and abs(oy + oh / 2 - y - h / 2) < 0.07 and 0.6 < ow / w < 1.6:
                near.setdefault(j, other)
        if len(near) > len(best):
            best = near
    if not best:
        return None, 0
    fx, fy, fw, fh = np.median(np.array(list(best.values())), axis=0)
    return (float(fx), float(fy), float(fw), float(fh)), len(best)


def edge_persistence(frames: list[np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
    """Per pixel, the share of frames in which it lies on a vertical / horizontal edge.
    The game changes from frame to frame; the border of an overlay is an edge in almost all of them."""
    vertical = np.zeros(frames[0].shape[:2], np.float32)
    horizontal = np.zeros_like(vertical)
    for frame in frames:
        gray = cv2.GaussianBlur(frame if frame.ndim == 2 else cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (3, 3), 0).astype(np.float32)
        vertical += np.abs(cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)) > EDGE_GRADIENT
        horizontal += np.abs(cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)) > EDGE_GRADIENT
    return vertical / len(frames), horizontal / len(frames)


def find_window(face: Box, vertical: np.ndarray, horizontal: np.ndarray) -> tuple[Box, float] | None:
    """The webcam rectangle around a face: the four persistent lines that enclose it best.
    Returns None when the overlay has no straight border (a cut-out streamer, a round frame)."""
    height, width = vertical.shape
    x0, x1 = int(face[0] * width), int((face[0] + face[2]) * width)
    y0, y1 = int(face[1] * height), int((face[1] + face[3]) * height)
    fw, fh = max(1, x1 - x0), max(1, y1 - y0)

    def candidates(strength: np.ndarray, lo: int, hi: int, limit: int) -> list[int]:
        lo, hi = max(0, lo), min(limit, hi)
        if hi <= lo:
            return []
        s = np.maximum(strength[lo:hi], np.roll(strength[lo:hi], 1))
        lines = [lo + int(i) for i in np.argsort(-s)[:8] if s[i] > MIN_LINE_PERSISTENCE]
        # The frame edge is a valid border when the search reaches it: overlays are often flush with it.
        return lines + ([0] if lo == 0 else []) + ([limit - 1] if hi == limit else [])

    cols = vertical[max(0, y0 - fh // 4) : y1 + fh // 4].mean(axis=0)
    rows = horizontal[:, max(0, x0 - fw // 4) : x1 + fw // 4].mean(axis=1)
    lefts = candidates(cols, x0 - int(2.5 * fw), x0 - fw // 8, width)
    rights = candidates(cols, x1 + fw // 8, x1 + int(2.5 * fw), width)
    tops = candidates(rows, y0 - int(1.6 * fh), y0 - fh // 8, height)
    bottoms = candidates(rows, y1 + fh // 4, y1 + int(3.2 * fh), height)

    def side_v(col: int, top: int, bottom: int) -> float:
        if col <= 1 or col >= width - 2:
            return 1.0
        return float(vertical[top:bottom, col - 1 : col + 2].max(axis=1).mean())

    def side_h(row: int, left: int, right: int) -> float:
        if row <= 1 or row >= height - 2:
            return 1.0
        return float(horizontal[row - 1 : row + 2, left:right].max(axis=0).mean())

    best: tuple[tuple[float, int], Box, float] | None = None
    for left in lefts:
        for right in rights:
            for top in tops:
                for bottom in bottoms:
                    w, h = right - left, bottom - top
                    if not (0.9 <= w / h <= 2.2) or w > 0.5 * width:
                        continue
                    score = min(side_v(left, top, bottom), side_v(right, top, bottom), side_h(top, left, right), side_h(bottom, left, right))
                    key = (round(score, 2), -w * h)
                    if best is None or key > best[0]:
                        best = (key, (left / width, top / height, w / width, h / height), score)
    if best is None or best[2] < MIN_WINDOW_SCORE:
        return None
    return best[1], best[2]


def detect_facecam(src: Path, duration_ms: int, work_dir: Path, samples: int = 24) -> dict:
    """Look for a streamer facecam: a face that stays in place across the video, then the window around it.

    ``rect`` is the measured webcam window when its borders are found (``window: "edges"``), else a
    head-and-shoulders box around the face (``window: "estimated"``).
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    frames: list[np.ndarray] = []
    for i in range(samples):
        frame_path = work_dir / f"facecam_{i}.jpg"
        try:
            ffmpeg.extract_frame(src, int(duration_ms * (i + 0.5) / samples), frame_path, width=960)
        except Exception:  # noqa: BLE001 - a missing sample only lowers the evidence
            continue
        image = cv2.imread(str(frame_path))
        frame_path.unlink(missing_ok=True)
        if image is not None and (not frames or image.shape == frames[0].shape):
            frames.append(image)
    out: dict = {"v": FACECAM_VERSION, "found": False, "samples": len(frames)}
    if len(frames) < 4:
        return out
    h, w = frames[0].shape[:2]
    detector = cv2.FaceDetectorYN.create(str(_YUNET), "", (w, h), FACE_SCORE, 0.3, 50)
    per_frame: list[list[Box]] = []
    for frame in frames:
        _, found = detector.detect(frame)
        per_frame.append([] if found is None else [(float(f[0]) / w, float(f[1]) / h, float(f[2]) / w, float(f[3]) / h) for f in found])
    face, hits = stable_face(per_frame)
    out["hits"] = hits
    if face is None or hits < MIN_FACE_HIT_RATIO * len(frames):
        return out
    fx, fy, fw, fh = face
    window = find_window(face, *edge_persistence(frames))
    if window is not None:
        (x, y, rw, rh), score = window
        x, y, rw, rh = x + rw * WINDOW_INSET, y + rh * WINDOW_INSET, rw * (1 - 2 * WINDOW_INSET), rh * (1 - 2 * WINDOW_INSET)
        out |= {"window": "edges", "score": round(score, 2)}
    else:
        if 0.3 < fx + fw / 2 < 0.7 and 0.25 < fy + fh / 2 < 0.75:
            return out | {"reason": "fullscreen_camera"}  # a face in the middle with no window: not an overlay
        rw, rh = min(1.0, fw * 2.6), min(1.0, fh * 2.4)
        x, y = clamp(fx + fw / 2 - rw / 2, 0, 1 - rw), clamp(fy - fh * 0.5, 0, 1)
        rh = min(rh, 1 - y)
        out["window"] = "estimated"
    return out | {
        "found": True,
        "rect": {"x": round(x, 4), "y": round(y, 4), "w": round(rw, 4), "h": round(rh, 4)},
        "face": {"x": round(fx, 4), "y": round(fy, 4), "w": round(fw, 4), "h": round(fh, 4)},
    }


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


def _cover(src: Rect, src_w: int, src_h: int, aspect: float, centre: tuple[float, float]) -> Rect:
    """The largest part of ``src`` with the given pixel aspect, kept as close to ``centre`` as it can be."""
    w, h = src.w, src.h
    if (w * src_w) / (h * src_h) > aspect:
        w = h * src_h * aspect / src_w
    else:
        h = w * src_w / aspect / src_h
    return Rect(x=clamp(centre[0] - w / 2, src.x, src.x + src.w - w), y=clamp(centre[1] - h / 2, src.y, src.y + src.h - h), w=w, h=h)


def _split(facecam: dict, position: str, src_w: int, src_h: int, out_w: int, out_h: int) -> tuple[list[Panel], int]:
    """Streamer and game stacked edge to edge, both filling the width: the usual short-form streamer layout.
    The webcam is cropped around the face when its shape does not fit its band; the game is cropped to its centre."""
    cam = Rect.model_validate(facecam["rect"])
    face = facecam.get("face") or facecam["rect"]
    cam_h = int(clamp(out_w * (cam.h * src_h) / (cam.w * src_w), out_h * SPLIT_CAM_MIN, out_h * SPLIT_CAM_MAX)) // 2 * 2
    game_h = out_h - cam_h
    cam_src = _cover(cam, src_w, src_h, out_w / cam_h, (face["x"] + face["w"] / 2, face["y"] + face["h"] / 2))
    game_src = _cover(Rect(x=0, y=0, w=1, h=1), src_w, src_h, out_w / game_h, (0.5, 0.5))
    if position == "bottom":
        panels = [Panel(src=game_src, x=0, y=0, w=out_w, h=game_h), Panel(src=cam_src, x=0, y=game_h, w=out_w, h=cam_h)]
        return panels, game_h - int(out_h * SPLIT_CAPTION_OFFSET)
    panels = [Panel(src=cam_src, x=0, y=0, w=out_w, h=cam_h), Panel(src=game_src, x=0, y=cam_h, w=out_w, h=game_h)]
    return panels, cam_h + int(out_h * SPLIT_CAPTION_OFFSET)


def resolve_layout(
    requested: str,
    *,
    src_w: int,
    src_h: int,
    out_w: int,
    out_h: int,
    duration_ms: int,
    facecam: dict | None = None,
    facecam_position: str = "top",
    manual: Rect | None = None,
    saliency: tuple[np.ndarray, np.ndarray] | None = None,
    cuts_ms: list[int] | None = None,
) -> ResolvedLayout:
    """Turn a layout request into concrete panels, falling back to the full-gameplay
    layout whenever the evidence needed for a tighter framing is weak or missing."""
    caption_w = int(out_w * (1 - 2 * SAFE_SIDE))
    cam = facecam if facecam and facecam.get("found") else None

    def result(used: str, reason: str, segments: list[LayoutSegment], caption_y: int) -> ResolvedLayout:
        return ResolvedLayout(requested=requested, used=used, reason=reason, segments=segments,  # type: ignore[arg-type]
                              caption_y=caption_y, caption_max_width=caption_w)  # fmt: skip

    def whole(panels: list[Panel]) -> list[LayoutSegment]:
        return [LayoutSegment(from_ms=0, to_ms=duration_ms, panels=panels)]

    full_panels, full_caption = _full(src_w, src_h, out_w, out_h)

    if requested == "manual" and manual is not None:
        panel = _fit_panel(manual, src_w, src_h, out_w, int(out_h * 0.66), int(out_h * SAFE_TOP))
        return result("manual", "recadrage manuel", whole([panel]), _caption_below(panel, out_h))

    if requested == "split" or (requested == "auto" and cam is not None):
        if cam is not None:
            panels, caption_y = _split(cam, facecam_position, src_w, src_h, out_w, out_h)
            where = "en bas" if facecam_position == "bottom" else "en haut"
            how = "fenêtre webcam mesurée" if cam.get("window") == "edges" else "cadre estimé autour du visage"
            return result("split", f"facecam {where} ({how}), jeu recadré au centre", whole(panels), caption_y)
        if requested == "split":
            return result("full", "aucune facecam fiable — gameplay complet utilisé à la place", whole(full_panels), full_caption)

    if requested == "crop":
        centre, confidence = crop_centre(*saliency) if saliency else (0.5, 0.0)
        if confidence < SALIENCY_MIN_CONFIDENCE:
            centre = 0.5
            reason = "position de l'action incertaine — recadrage laissé au centre"
        else:
            reason = f"recadrage centré sur l'action (confiance {confidence:.2f})"
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
            return result("dynamic", f"{len(segments)} cadrage(s), changés aux changements de plan", segments, max(full_caption, crop_caption))
        return result("full", "aucun plan avec une action nettement localisée — gameplay complet conservé", whole(full_panels), full_caption)

    return result("full", "gameplay complet préservé", whole(full_panels), full_caption)
