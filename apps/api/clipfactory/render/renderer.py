"""Clip production: FFmpeg cut → layout → Remotion composition → validated export."""

from __future__ import annotations

import json
import re
import shutil
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from clipfactory.config import RENDERER_DIR, Settings
from clipfactory.domain import Caption, ExportValidation, Rect, ResolvedLayout
from clipfactory.errors import ExportInvalid, RenderError, SubprocessFailed
from clipfactory.media import ffmpeg
from clipfactory.media.proc import run
from clipfactory.media.validate import validate_export
from clipfactory.render import captions as cap
from clipfactory.render.layout import resolve_layout, saliency_profile
from clipfactory.timeutil import frames_for

PREVIEW_SCALE = 0.5


@dataclass
class RenderSpec:
    """Everything needed to render one clip — plain data, detached from the database."""

    clip_id: str
    source_path: Path
    source_fingerprint: str
    src_has_audio: bool
    start_ms: int
    end_ms: int
    width: int
    height: int
    fps: int
    layout: str
    manual_rect: Rect | None
    facecam: dict | None
    cuts_ms: list[int]  # scene cuts on the source timeline
    subtitle_style: str
    subtitle_options: dict
    captions: list[Caption]
    burn_subtitles: bool
    mode: str  # preview | final
    remotion_concurrency: int | None = None


@dataclass
class RenderedFile:
    path: Path
    cut_path: Path
    layout: ResolvedLayout
    expected_ms: int
    width: int
    height: int
    fps: int
    render_seconds: float


def slugify(text: str, fallback: str = "clip") -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_text).strip("-").lower()[:48]
    return slug or fallback


def _ensure_cut(settings: Settings, spec: RenderSpec, work: Path, on_progress: Callable[[float], None], should_stop) -> Path:  # noqa: ANN001
    """The cut depends only on the source and the timestamps, so restyling a clip never re-cuts it."""
    cut, bg, meta = work / "cut.mp4", work / "bg.mp4", work / "cut.json"
    key = {"fingerprint": spec.source_fingerprint, "start_ms": spec.start_ms, "end_ms": spec.end_ms, "fps": spec.fps}
    if cut.exists() and bg.exists() and meta.exists() and json.loads(meta.read_text()) == key:
        return cut
    meta.unlink(missing_ok=True)
    ffmpeg.cut_clip(spec.source_path, cut, start_ms=spec.start_ms, end_ms=spec.end_ms, fps=spec.fps,
                    has_audio=spec.src_has_audio, on_progress=on_progress, should_stop=should_stop)  # fmt: skip
    ffmpeg.make_background(cut, bg, fps=spec.fps)
    meta.write_text(json.dumps(key))
    return cut


def render_clip(
    settings: Settings,
    spec: RenderSpec,
    *,
    on_progress: Callable[[float, str], None],
    should_stop: Callable[[], None],
) -> RenderedFile:
    work = settings.path("work", spec.clip_id)
    work.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()

    on_progress(0.0, "découpe image par image")
    cut = _ensure_cut(settings, spec, work, lambda f: on_progress(0.2 * f, "découpe image par image"), should_stop)
    cut_info = ffmpeg.probe(cut)
    duration_ms = spec.end_ms - spec.start_ms
    total_frames = frames_for(spec.start_ms, spec.end_ms, spec.fps)

    saliency = saliency_profile(cut) if spec.layout in ("crop", "dynamic") else None
    layout = resolve_layout(
        spec.layout, src_w=cut_info.width, src_h=cut_info.height, out_w=spec.width, out_h=spec.height,
        duration_ms=duration_ms, facecam=spec.facecam, manual=spec.manual_rect, saliency=saliency,
        cuts_ms=[c - spec.start_ms for c in spec.cuts_ms if spec.start_ms < c < spec.end_ms],
    )  # fmt: skip

    props = {
        "src": "cut.mp4",
        "background": "bg.mp4",
        "width": spec.width,
        "height": spec.height,
        "fps": spec.fps,
        "durationInFrames": total_frames,
        "hasAudio": spec.src_has_audio,
        "layout": layout.model_dump(),
        "captions": [c.model_dump(exclude_none=True) | {"timestampMs": c.timestampMs, "confidence": c.confidence}
                     for c in spec.captions] if spec.burn_subtitles else [],
        "style": spec.subtitle_style,
        "options": spec.subtitle_options,
    }  # fmt: skip
    props_file = work / f"props_{spec.mode}.json"
    props_file.write_text(json.dumps(props, ensure_ascii=False))
    out = work / f"out_{spec.mode}.mp4"
    out.unlink(missing_ok=True)

    def on_line(line: str) -> None:
        if line.startswith("{"):
            try:
                event = json.loads(line)
            except ValueError:
                return
            if event.get("type") == "progress":
                on_progress(0.2 + 0.8 * float(event["value"]), "composition Remotion")

    cmd = ["node", str(RENDERER_DIR / "render.mjs"), "--props", str(props_file), "--out", str(out), "--root", str(work)]
    if spec.mode == "preview":
        cmd += ["--scale", str(PREVIEW_SCALE), "--crf", "28"]
    if spec.remotion_concurrency:
        cmd += ["--concurrency", str(spec.remotion_concurrency)]
    try:
        run(cmd, timeout=3600, on_line=on_line, should_stop=should_stop, cwd=str(RENDERER_DIR))
    except SubprocessFailed as exc:
        raise RenderError("Remotion render failed", stderr=str(exc.details.get("stderr", ""))[-1500:]) from exc
    scale = PREVIEW_SCALE if spec.mode == "preview" else 1.0
    return RenderedFile(
        path=out, cut_path=cut, layout=layout, expected_ms=round(total_frames * 1000 / spec.fps),
        width=int(spec.width * scale), height=int(spec.height * scale), fps=spec.fps,
        render_seconds=time.monotonic() - started,
    )  # fmt: skip


def validate_render(spec: RenderSpec, rendered: RenderedFile) -> ExportValidation:
    return validate_export(
        rendered.path, expected_ms=rendered.expected_ms, width=rendered.width, height=rendered.height,
        fps=rendered.fps, expect_audio=spec.src_has_audio,
        captions=spec.captions if spec.burn_subtitles else None, reference_cut=rendered.cut_path,
    )  # fmt: skip


def publish_export(
    settings: Settings,
    spec: RenderSpec,
    rendered: RenderedFile,
    validation: ExportValidation,
    *,
    project_id: str,
    stem: str,
    metadata: dict,
    extra_copy_dir: str | None = None,
) -> dict[str, Path]:
    """Move a *validated* render to its final place and write its sidecars. Refuses anything else."""
    if not validation.ok:
        raise ExportInvalid("render failed validation", failures=validation.failures, details=validation.details)
    if spec.mode == "preview":
        dst = settings.path("previews", f"{spec.clip_id}.mp4")
        shutil.move(str(rendered.path), dst)
        return {"preview": dst}
    folder = settings.path("exports", project_id)
    folder.mkdir(parents=True, exist_ok=True)
    for stale in folder.glob(f"*_{spec.clip_id[:8]}.*"):
        stale.unlink()
    base = f"{stem}_{spec.clip_id[:8]}"
    video = folder / f"{base}.mp4"
    shutil.move(str(rendered.path), video)
    srt = folder / f"{base}.srt"
    srt.write_text(cap.to_srt(spec.captions), encoding="utf-8")
    meta = folder / f"{base}.json"
    meta.write_text(json.dumps(metadata, ensure_ascii=False, indent=1), encoding="utf-8")
    files = {"export": video, "srt": srt, "json": meta}
    if extra_copy_dir:
        target = Path(extra_copy_dir).expanduser()
        target.mkdir(parents=True, exist_ok=True)
        for f in files.values():
            shutil.copy2(f, target / f.name)
    return files
