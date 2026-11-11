"""Clip gallery, editor actions, feedback and downloads."""

from __future__ import annotations

import tempfile
import zipfile
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from clipfactory.api.serializers import clip_out, job_out
from clipfactory.config import get_settings
from clipfactory.db import get_session
from clipfactory.domain import REJECTION_REASONS, Caption, FacecamPosition, Layout, Rect, SubtitleOptions, SubtitleStyle
from clipfactory.errors import Conflict, NotFound, ValidationFailed
from clipfactory.models import Candidate, Clip, Feedback, Job, MediaAsset, Project
from clipfactory.pipeline.runner import create_job
from clipfactory.render.captions import apply_page_edits, build_captions
from clipfactory.render.renderer import slugify
from clipfactory.timeutil import clock
from clipfactory.transcription.service import latest_transcript, segments_of

router = APIRouter()
MIN_CLIP_MS = 2000
MAX_CLIP_MS = 180_000


def _clip(s: Session, clip_id: str) -> Clip:
    clip = s.get(Clip, clip_id)
    if clip is None:
        raise NotFound("clip not found")
    return clip


@router.get("/clips")
def list_clips(
    project_id: str | None = None,
    approval: Literal["pending", "approved", "rejected"] | None = None,
    sort: Literal["score", "recent", "duration", "rank"] = "score",
    s: Session = Depends(get_session),
) -> dict:
    query = select(Clip)
    if project_id:
        query = query.where(Clip.project_id == project_id)
    if approval:
        query = query.where(Clip.approval == approval)
    order = {"score": Clip.score.desc(), "recent": Clip.created_at.desc(), "duration": (Clip.end_ms - Clip.start_ms).desc(),
             "rank": Clip.rank}[sort]  # fmt: skip
    clips = list(s.scalars(query.order_by(order).limit(500)))
    names = dict(s.execute(select(Project.id, Project.name).where(Project.id.in_({c.project_id for c in clips}))).all())
    return {"clips": [clip_out(s, c) | {"project_name": names.get(c.project_id, "")} for c in clips]}


@router.get("/clips/{clip_id}")
def get_clip(clip_id: str, s: Session = Depends(get_session)) -> dict:
    clip = _clip(s, clip_id)
    project = clip.project
    candidate = s.get(Candidate, clip.candidate_id) if clip.candidate_id else None
    active = s.scalar(select(Job).where(Job.clip_id == clip.id, Job.status.in_(("queued", "running"))).order_by(Job.created_at.desc()))
    return {
        "clip": clip_out(s, clip, detail=True),
        "source": {"id": project.source.id, "title": project.source.title, "duration_ms": project.source.duration_ms,
                   "width": project.source.width, "height": project.source.height, "origin_url": project.source.origin_url,
                   "facecam_found": bool((project.source.facecam or {}).get("found"))},  # fmt: skip
        "project": {"id": project.id, "name": project.name, "settings": project.settings},
        "evidence": candidate.evidence if candidate else None,
        "penalties": candidate.penalties if candidate else None,
        "active_job": job_out(s, active) if active else None,
    }


class ClipPatch(BaseModel):
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, ge=0)
    layout: Layout | None = None
    manual_crop: Rect | None = None
    facecam_position: FacecamPosition | None = None
    subtitle_style: SubtitleStyle | None = None
    subtitle_options: SubtitleOptions | None = None
    caption_edits: dict[int, str] | None = None  # page index → new text
    reset_captions: bool = False
    title: str | None = Field(default=None, max_length=300)
    social_caption: str | None = Field(default=None, max_length=500)
    hashtags: list[str] | None = None


@router.patch("/clips/{clip_id}")
def patch_clip(clip_id: str, body: ClipPatch, s: Session = Depends(get_session)) -> dict:
    """Apply editor changes. Anything that alters the picture marks the export stale;
    nothing is rendered until the user asks for it."""
    clip = _clip(s, clip_id)
    source = clip.project.source
    visual_change = False
    rebuild = body.reset_captions

    if body.start_ms is not None or body.end_ms is not None:
        start = clip.start_ms if body.start_ms is None else body.start_ms
        end = clip.end_ms if body.end_ms is None else body.end_ms
        end = min(end, source.duration_ms or end)
        if not (MIN_CLIP_MS <= end - start <= MAX_CLIP_MS):
            raise ValidationFailed(f"a clip must last between {MIN_CLIP_MS // 1000} and {MAX_CLIP_MS // 1000} seconds")
        if (start, end) != (clip.start_ms, clip.end_ms):
            clip.start_ms, clip.end_ms = start, end
            visual_change = rebuild = True  # captions are clip-relative: new bounds need new captions
    if body.layout is not None and body.layout != clip.layout:
        if body.layout == "manual" and body.manual_crop is None and not (clip.layout_params or {}).get("manual"):
            raise ValidationFailed("the manual layout needs a crop rectangle")
        clip.layout, visual_change = body.layout, True
    if body.manual_crop is not None:
        clip.layout_params = {**(clip.layout_params or {}), "manual": body.manual_crop.model_dump()}
        visual_change = True
    if body.facecam_position is not None and body.facecam_position != (clip.layout_params or {}).get("facecam_position"):
        clip.layout_params = {**(clip.layout_params or {}), "facecam_position": body.facecam_position}
        visual_change = True
    if body.subtitle_style is not None and body.subtitle_style != clip.subtitle_style:
        clip.subtitle_style, visual_change = body.subtitle_style, True
        rebuild = rebuild or not clip.captions_edited  # page length depends on the style
    if body.subtitle_options is not None:
        clip.subtitle_options, visual_change = body.subtitle_options.model_dump(), True

    if rebuild:
        transcript = latest_transcript(s, source.id)
        captions, precision = build_captions(segments_of(transcript) if transcript else [], clip.start_ms, clip.end_ms, clip.subtitle_style)
        clip.captions, clip.captions_precision, clip.captions_edited = [c.model_dump() for c in captions], precision, False
        visual_change = True
    if body.caption_edits:
        current = [Caption.model_validate(c) for c in clip.captions or []]
        edited = apply_page_edits(current, {int(k): v for k, v in body.caption_edits.items()})
        clip.captions, clip.captions_edited, visual_change = [c.model_dump() for c in edited], True, True

    if body.title is not None:
        clip.title = body.title
    if body.title is not None or body.social_caption is not None or body.hashtags is not None:
        social = dict(clip.social or {})
        if body.title is not None:
            social["title"] = body.title
        if body.social_caption is not None:
            social["caption"] = body.social_caption
        if body.hashtags is not None:
            social["hashtags"] = [h if h.startswith("#") else f"#{h}" for h in body.hashtags if h.strip()][:10]
        clip.social = social
    if visual_change:
        clip.edited = True
        if clip.render_status in ("rendered", "failed"):
            clip.render_status = "stale"
    s.flush()
    return {"clip": clip_out(s, clip, detail=True)}


class RenderRequest(BaseModel):
    mode: Literal["preview", "final"] = "final"


@router.post("/clips/{clip_id}/render")
def render(clip_id: str, body: RenderRequest, s: Session = Depends(get_session)) -> dict:
    clip = _clip(s, clip_id)
    busy = s.scalar(select(Job.id).where(Job.clip_id == clip.id, Job.status.in_(("queued", "running"))))
    pipeline_busy = s.scalar(select(Job.id).where(Job.project_id == clip.project_id, Job.kind == "pipeline", Job.status.in_(("queued", "running"))))
    if busy or pipeline_busy:
        raise Conflict("this clip is already being rendered")
    # Previews jump the queue: they are what the user is waiting on while editing.
    job = create_job(s, project_id=clip.project_id, kind="render", clip_id=clip.id, is_test=clip.is_test,
                     payload={"clip_id": clip.id, "mode": body.mode}, priority=20 if body.mode == "preview" else 10)  # fmt: skip
    if body.mode == "final":
        clip.render_status = "queued"
    s.flush()
    return {"job": job_out(s, job)}


class FeedbackIn(BaseModel):
    decision: Literal["accepted", "rejected", "reset", "rated", "metrics"]
    rejection_reason: str | None = None
    rating: int | None = Field(default=None, ge=1, le=5)
    social_metrics: dict[str, int] | None = None  # e.g. {"views": 12000, "likes": 800} — entered by hand
    note: str | None = Field(default=None, max_length=1000)


@router.post("/clips/{clip_id}/feedback")
def feedback(clip_id: str, body: FeedbackIn, s: Session = Depends(get_session)) -> dict:
    clip = _clip(s, clip_id)
    if body.rejection_reason and body.rejection_reason not in REJECTION_REASONS:
        raise ValidationFailed("unknown rejection reason", allowed=list(REJECTION_REASONS))
    if body.decision in ("accepted", "rejected", "reset"):
        clip.approval = {"accepted": "approved", "rejected": "rejected", "reset": "pending"}[body.decision]
    candidate = s.get(Candidate, clip.candidate_id) if clip.candidate_id else None
    if body.decision != "reset":
        # Append-only: every decision is kept with the state of the clip at that moment.
        s.add(Feedback(
            clip_id=clip.id, decision=body.decision, rejection_reason=body.rejection_reason, rating=body.rating,
            edited=clip.edited, profile=(clip.project.settings or {}).get("profile"),
            event_type=candidate.event_type if candidate else None, original_score=clip.score,
            final_start_ms=clip.start_ms, final_end_ms=clip.end_ms, layout=clip.layout, subtitle_style=clip.subtitle_style,
            social_metrics=body.social_metrics, note=body.note,
        ))  # fmt: skip
    s.flush()
    return {"clip": clip_out(s, clip, detail=True)}


def _asset(s: Session, clip_id: str, kind: str) -> tuple[MediaAsset, Path]:
    asset = s.scalar(select(MediaAsset).where(MediaAsset.clip_id == clip_id, MediaAsset.kind == kind))
    if asset is None:
        raise NotFound(f"this clip has no {kind} file yet")
    path = get_settings().path(asset.path)
    if not path.exists():
        raise NotFound("the file is no longer on disk")
    return asset, path


@router.get("/clips/{clip_id}/download")
def download(clip_id: str, kind: Literal["export", "srt", "json", "preview"] = "export", s: Session = Depends(get_session)) -> FileResponse:
    _clip(s, clip_id)
    asset, path = _asset(s, clip_id, kind)
    return FileResponse(path, media_type=asset.mime, filename=path.name)


def post_text(clip: Clip, source_title: str, origin_url: str | None) -> str:
    social = clip.social or {}
    lines = [social.get("title") or clip.title, "", social.get("caption") or "", " ".join(social.get("hashtags") or []), "",
             f"Source : {source_title}" + (f" — {origin_url}" if origin_url else ""),
             f"Extrait : {clock(clip.start_ms)} → {clock(clip.end_ms)}"]  # fmt: skip
    return "\n".join(lines).strip() + "\n"


@router.get("/clips-zip")
def download_zip(ids: str | None = None, project_id: str | None = None, approved_only: bool = False,
                 s: Session = Depends(get_session)) -> FileResponse:  # fmt: skip
    """Bulk export: one folder per clip with the MP4, subtitles, metadata and ready-to-paste post text."""
    query = select(Clip)
    if ids:
        query = query.where(Clip.id.in_([i for i in ids.split(",") if i][:200]))
    elif project_id:
        query = query.where(Clip.project_id == project_id)
    else:
        raise ValidationFailed("provide clip ids or a project id")
    if approved_only:
        query = query.where(Clip.approval == "approved")
    clips = [c for c in s.scalars(query.order_by(Clip.project_id, Clip.rank))
             if s.scalar(select(MediaAsset.id).where(MediaAsset.clip_id == c.id, MediaAsset.kind == "export"))]  # fmt: skip
    if not clips:
        raise NotFound("none of the selected clips has a finished export")
    settings = get_settings()
    tmp = Path(tempfile.mkstemp(suffix=".zip", dir=settings.path("work"))[1])
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as archive:  # MP4 is already compressed
        for clip in clips:
            folder = f"{clip.rank:02d}_{slugify(clip.title)}_{clip.id[:8]}"
            for kind in ("export", "srt", "json"):
                try:
                    _, path = _asset(s, clip.id, kind)
                except NotFound:
                    continue
                archive.write(path, f"{folder}/{path.name}")
            source = clip.project.source
            archive.writestr(f"{folder}/post.txt", post_text(clip, source.title, source.origin_url))
    return FileResponse(tmp, media_type="application/zip", filename="clipfactory_clips.zip",
                        background=BackgroundTask(tmp.unlink, missing_ok=True))  # fmt: skip
