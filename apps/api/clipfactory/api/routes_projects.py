"""Sources, projects, jobs and transcripts."""

from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from clipfactory.api.serializers import iso, job_out, project_out, source_out
from clipfactory.config import get_settings
from clipfactory.db import get_session
from clipfactory.domain import ProjectSettings
from clipfactory.errors import Conflict, MediaError, NotFound, ValidationFailed
from clipfactory.media import ingest
from clipfactory.models import Clip, Job, JobLog, Project, Source, new_id
from clipfactory.pipeline.common import purge_clip_files
from clipfactory.pipeline.runner import TERMINAL, create_job, reset_for_retry
from clipfactory.render.captions import build_captions
from clipfactory.transcription.service import export_transcript_json, latest_transcript, segments_of

router = APIRouter()
_CHUNK = 4 * 1024 * 1024


# ── sources ─────────────────────────────────────────────────────────────────


@router.post("/sources/upload")
async def upload_source(request: Request, filename: str, s: Session = Depends(get_session)) -> dict:
    """Raw-body upload streamed straight to disk (no multipart buffering), capped in size."""
    settings = get_settings()
    ext = ingest.validated_extension(filename)
    limit = int(settings.clipfactory_max_upload_gb * 1024**3)
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise ValidationFailed(f"file is larger than the {settings.clipfactory_max_upload_gb:g} GB limit")
    source = Source(id=new_id(), kind="upload", title=ingest.safe_title(filename).rsplit(".", 1)[0],
                    original_filename=ingest.safe_title(filename), rights_confirmed=True)  # fmt: skip
    path = settings.path("sources", f"{source.id}{ext}")  # the on-disk name never comes from user input
    written = 0
    try:
        with path.open("wb") as out:
            async for chunk in request.stream():
                written += len(chunk)
                if written > limit:
                    raise ValidationFailed(f"file is larger than the {settings.clipfactory_max_upload_gb:g} GB limit")
                out.write(chunk)
        if written == 0:
            raise ValidationFailed("the upload was empty")
        s.add(source)
        s.flush()
        final = ingest.finalize_source(s, settings, source, path)
    except (ValidationFailed, MediaError):
        path.unlink(missing_ok=True)
        raise
    s.flush()
    return {"source": source_out(s, final), "duplicate": final.id != source.id}


class YouTubeInspect(BaseModel):
    url: str = Field(max_length=300)


@router.post("/sources/youtube/inspect")
def inspect_youtube(body: YouTubeInspect) -> dict:
    return ingest.inspect_youtube(body.url).model_dump()


@router.get("/sources")
def list_sources(s: Session = Depends(get_session)) -> dict:
    rows = s.scalars(select(Source).order_by(Source.created_at.desc()))
    return {"sources": [source_out(s, src) | {"projects": len(src.projects)} for src in rows]}


@router.delete("/sources/{source_id}")
def delete_source(source_id: str, s: Session = Depends(get_session)) -> dict:
    """Remove a source, its projects, clips, and every derived file."""
    source = s.get(Source, source_id)
    if source is None:
        raise NotFound("source not found")
    active = [j for p in source.projects for j in p.jobs if j.status in ("queued", "running")]
    if active:
        raise Conflict("a job is still running on this source; cancel it first")
    settings = get_settings()
    asset_paths = [a.path for a in source.assets]
    for project in source.projects:
        for clip in project.clips:
            purge_clip_files(settings, s, clip)
        export_dir = settings.path("exports", project.id)
        if export_dir.exists():
            for f in export_dir.iterdir():
                f.unlink()
            export_dir.rmdir()
    freed = ingest.delete_source_files(settings, source.id, source.path, asset_paths)
    s.delete(source)
    return {"deleted": source_id, "freed_bytes": freed}


# ── projects ────────────────────────────────────────────────────────────────


class ProjectCreate(BaseModel):
    source_id: str | None = None
    youtube_url: str | None = Field(default=None, max_length=300)
    rights_confirmed: bool = False
    name: str | None = Field(default=None, max_length=200)
    settings: ProjectSettings = ProjectSettings()


@router.post("/projects")
def create_project(body: ProjectCreate, s: Session = Depends(get_session)) -> dict:
    settings = get_settings()
    if body.source_id:
        source = s.get(Source, body.source_id)
        if source is None or source.status != "ready":
            raise NotFound("source not found or not ready")
    elif body.youtube_url:
        if not body.rights_confirmed:
            raise ValidationFailed("confirm that you own this video or are authorised to download and reuse it")
        info = ingest.inspect_youtube(body.youtube_url)
        if info.is_live:
            raise ValidationFailed("live streams cannot be imported until they have ended")
        if info.duration_s and info.duration_s > ingest.MAX_YOUTUBE_DURATION_S:
            raise ValidationFailed("this video is longer than the 12 hour import limit")
        source = s.scalar(select(Source).where(Source.origin_url == info.url, Source.status.in_(("ready", "pending"))))
        if source is None:
            source = Source(kind="youtube", title=info.title, channel=info.channel, origin_url=info.url, rights_confirmed=True,
                            duration_ms=info.duration_s * 1000 if info.duration_s else None)  # fmt: skip
            s.add(source)
            s.flush()
    else:
        raise ValidationFailed("provide either an uploaded source or a YouTube URL")
    project = Project(source_id=source.id, name=(body.name or source.title or "Projet")[:300],
                      settings=body.settings.model_dump(mode="json"), is_test=settings.test_mode)  # fmt: skip
    s.add(project)
    s.flush()
    create_job(s, project_id=project.id, kind="pipeline", is_test=settings.test_mode)
    s.flush()
    s.refresh(project)
    return {"project": project_out(s, project)}


@router.get("/projects")
def list_projects(s: Session = Depends(get_session)) -> dict:
    rows = s.scalars(select(Project).order_by(Project.created_at.desc()).limit(200))
    return {"projects": [project_out(s, p) for p in rows]}


def _project(s: Session, project_id: str) -> Project:
    project = s.get(Project, project_id)
    if project is None:
        raise NotFound("project not found")
    return project


@router.get("/projects/{project_id}")
def get_project(project_id: str, s: Session = Depends(get_session)) -> dict:
    return {"project": project_out(s, _project(s, project_id), detail=True)}


@router.delete("/projects/{project_id}")
def delete_project(project_id: str, s: Session = Depends(get_session)) -> dict:
    project = _project(s, project_id)
    if any(j.status in ("queued", "running") for j in project.jobs):
        raise Conflict("a job is still running on this project; cancel it first")
    settings = get_settings()
    for clip in project.clips:
        purge_clip_files(settings, s, clip)
    export_dir = settings.path("exports", project.id)
    if export_dir.exists():
        for f in export_dir.iterdir():
            f.unlink()
        export_dir.rmdir()
    s.delete(project)
    return {"deleted": project_id}


class Rerun(BaseModel):
    from_stage: Literal["transcribe", "detect_events", "analyze", "rank", "refine", "render"] = "analyze"
    settings: ProjectSettings | None = None


@router.post("/projects/{project_id}/rerun")
def rerun_project(project_id: str, body: Rerun, s: Session = Depends(get_session)) -> dict:
    """Run the pipeline again from a given stage, reusing everything upstream of it."""
    project = _project(s, project_id)
    job = next((j for j in sorted(project.jobs, key=lambda j: j.created_at, reverse=True) if j.kind == "pipeline"), None)
    if job is None:
        raise NotFound("this project has no pipeline job")
    if job.status not in TERMINAL:
        raise Conflict("the pipeline is still running")
    if body.settings is not None:
        project.settings = body.settings.model_dump(mode="json")
    reset_for_retry(s, job, body.from_stage)
    return {"job": job_out(s, job)}


@router.get("/projects/{project_id}/performance")
def performance(project_id: str, s: Session = Depends(get_session)) -> dict:
    _project(s, project_id)
    path = get_settings().path("exports", project_id, "performance.json")
    if not path.exists():
        raise NotFound("no performance report yet — it is written when a pipeline run completes")
    return json.loads(path.read_text())


# ── jobs ────────────────────────────────────────────────────────────────────


def _job(s: Session, job_id: str) -> Job:
    job = s.get(Job, job_id)
    if job is None:
        raise NotFound("job not found")
    return job


@router.get("/jobs/{job_id}")
def get_job(job_id: str, s: Session = Depends(get_session)) -> dict:
    return {"job": job_out(s, _job(s, job_id))}


@router.get("/jobs/{job_id}/logs")
def job_logs(job_id: str, after: int = 0, s: Session = Depends(get_session)) -> dict:
    rows = s.scalars(select(JobLog).where(JobLog.job_id == job_id, JobLog.id > after).order_by(JobLog.id).limit(500))
    return {"logs": [{"id": r.id, "stage": r.stage, "level": r.level, "message": r.message, "ts": iso(r.ts)} for r in rows]}


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str, s: Session = Depends(get_session)) -> dict:
    job = _job(s, job_id)
    if job.status == "queued":
        job.status = "cancelled"
    elif job.status == "running":
        job.cancel_requested = True  # the worker stops at its next checkpoint (sub-second in practice)
    else:
        raise Conflict(f"job is already {job.status}")
    return {"job": job_out(s, job)}


@router.post("/jobs/{job_id}/retry")
def retry_job(job_id: str, s: Session = Depends(get_session)) -> dict:
    job = _job(s, job_id)
    if job.status not in ("failed", "cancelled"):
        raise Conflict("only failed or cancelled jobs can be retried")
    reset_for_retry(s, job)
    return {"job": job_out(s, job)}


# ── transcript ──────────────────────────────────────────────────────────────


@router.get("/sources/{source_id}/transcript")
def get_transcript(source_id: str, start_ms: int = 0, end_ms: int | None = None, s: Session = Depends(get_session)) -> dict:
    transcript = latest_transcript(s, source_id)
    if transcript is None:
        raise NotFound("no transcript for this source yet")
    segments = [sg for sg in transcript.segments if sg.end_ms > start_ms and (end_ms is None or sg.start_ms < end_ms)]
    return {
        "transcript": {"id": transcript.id, "provider": transcript.provider, "model": transcript.model,
                       "language": transcript.language, "precision": transcript.precision, "edited": transcript.edited,
                       "is_test": transcript.is_test},  # fmt: skip
        "segments": [{"id": sg.id, "start_ms": sg.start_ms, "end_ms": sg.end_ms, "text": sg.text, "language": sg.language,
                      "confidence": sg.confidence, "has_words": bool(sg.words), "provenance": sg.provenance} for sg in segments],  # fmt: skip
    }


class SegmentEdit(BaseModel):
    id: str
    text: str = Field(max_length=2000)


class TranscriptEdit(BaseModel):
    segments: list[SegmentEdit]


@router.put("/sources/{source_id}/transcript")
def edit_transcript(source_id: str, body: TranscriptEdit, s: Session = Depends(get_session)) -> dict:
    """Correct transcript text. Subtitles of affected clips are rebuilt from the corrected
    transcript and flagged for re-render — no transcription or analysis is repeated."""
    transcript = latest_transcript(s, source_id)
    if transcript is None:
        raise NotFound("no transcript for this source yet")
    by_id = {sg.id: sg for sg in transcript.segments}
    touched: list[tuple[int, int]] = []
    for edit in body.segments:
        seg = by_id.get(edit.id)
        text = " ".join(edit.text.split())
        if seg is None or text == seg.text:
            continue
        new_words = text.split()
        if seg.words and len(new_words) == len(seg.words):
            seg.words = [{**w, "w": " " + nw} for w, nw in zip(seg.words, new_words, strict=True)]  # timing kept word for word
        else:
            seg.words = None  # the word count changed: the old word timings no longer describe this text
        seg.text = text
        seg.provenance = "user-edit"
        touched.append((seg.start_ms, seg.end_ms))
    if not touched:
        return {"updated_segments": 0, "clips_to_rerender": 0}
    transcript.edited = True
    if any(sg.words is None for sg in transcript.segments):
        transcript.precision = "segment"
    s.flush()
    export_transcript_json(s, get_settings(), transcript)
    segments = segments_of(transcript)
    stale = 0
    clips = s.scalars(select(Clip).join(Project, Project.id == Clip.project_id).where(Project.source_id == source_id))
    for clip in clips:
        if clip.captions_edited or not any(a < clip.end_ms and b > clip.start_ms for a, b in touched):
            continue
        captions, precision = build_captions(segments, clip.start_ms, clip.end_ms, clip.subtitle_style)
        clip.captions = [c.model_dump() for c in captions]
        clip.captions_precision = precision
        if clip.render_status == "rendered":
            clip.render_status = "stale"
        stale += 1
    return {"updated_segments": len(touched), "clips_to_rerender": stale}

