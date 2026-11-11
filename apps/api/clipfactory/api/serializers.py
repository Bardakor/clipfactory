"""ORM → JSON for the dashboard. Nothing here ever includes a filesystem path or a secret."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from clipfactory.domain import Caption
from clipfactory.models import Clip, InferenceUsage, Job, MediaAsset, Project, Source
from clipfactory.render.captions import pages_of


def iso(dt) -> str | None:  # noqa: ANN001
    return dt.isoformat() + "Z" if dt else None


def media_url(asset_id: str | None, version: object = None) -> str | None:
    if not asset_id:
        return None
    return f"/api/media/{asset_id}" + (f"?v={version}" if version else "")


def source_out(s: Session, src: Source) -> dict:
    thumb = s.scalar(select(MediaAsset.id).where(MediaAsset.source_id == src.id, MediaAsset.kind == "thumbnail"))
    return {
        "id": src.id, "kind": src.kind, "status": src.status, "title": src.title, "channel": src.channel,
        "origin_url": src.origin_url, "original_filename": src.original_filename, "bytes": src.bytes,
        "duration_ms": src.duration_ms, "fps": src.fps, "width": src.width, "height": src.height,
        "video_codec": src.video_codec, "audio_codec": src.audio_codec, "audio_channels": src.audio_channels,
        "tracks": (src.probe or {}).get("tracks", []), "facecam": src.facecam, "thumbnail_url": media_url(thumb),
        "error": src.error, "created_at": iso(src.created_at),
    }  # fmt: skip


def stage_out(st) -> dict:  # noqa: ANN001
    return {
        "name": st.name, "status": st.status, "progress": st.progress, "detail": st.detail, "cache_hit": st.cache_hit,
        "metrics": st.metrics, "error": st.error, "started_at": iso(st.started_at), "finished_at": iso(st.finished_at),
        "duration_ms": st.duration_ms,
    }  # fmt: skip


def job_cost(s: Session, job_id: str) -> dict:
    actual, estimated, requests = s.execute(
        select(func.sum(InferenceUsage.cost_usd_actual), func.sum(InferenceUsage.cost_usd_estimated), func.count())
        .where(InferenceUsage.job_id == job_id, InferenceUsage.status == "ok")
    ).one()  # fmt: skip
    return {"actual_usd": round(actual or 0, 6), "estimated_usd": round(estimated or 0, 6), "requests": requests}


def job_out(s: Session, job: Job) -> dict:
    return {
        "id": job.id, "project_id": job.project_id, "clip_id": job.clip_id, "kind": job.kind, "status": job.status,
        "attempt": job.attempt, "cancel_requested": job.cancel_requested, "error": job.error, "warnings": job.warnings or [],
        "is_test": job.is_test, "payload": job.payload, "created_at": iso(job.created_at), "started_at": iso(job.started_at),
        "finished_at": iso(job.finished_at), "stages": [stage_out(st) for st in job.stages], "cost": job_cost(s, job.id),
    }  # fmt: skip


def clip_out(s: Session, clip: Clip, *, detail: bool = False) -> dict:
    assets = {a.kind: a for a in s.scalars(select(MediaAsset).where(MediaAsset.clip_id == clip.id))}
    version = int(clip.updated_at.timestamp()) if clip.updated_at else None
    out = {
        "id": clip.id, "project_id": clip.project_id, "rank": clip.rank, "start_ms": clip.start_ms, "end_ms": clip.end_ms,
        "original_start_ms": clip.original_start_ms, "original_end_ms": clip.original_end_ms,
        "duration_ms": clip.end_ms - clip.start_ms, "score": clip.score, "score_basis": clip.score_basis,
        "title": clip.title, "main_event": clip.main_event, "justification": clip.justification, "layout": clip.layout,
        "layout_params": clip.layout_params, "resolved_layout": {k: v for k, v in (clip.resolved_layout or {}).items() if k in ("used", "reason")} or None,
        "subtitle_style": clip.subtitle_style, "subtitle_options": clip.subtitle_options,
        "captions_precision": clip.captions_precision, "captions_edited": clip.captions_edited, "social": clip.social,
        "render_status": clip.render_status, "approval": clip.approval, "edited": clip.edited, "is_test": clip.is_test,
        "validation": clip.validation, "created_at": iso(clip.created_at), "updated_at": iso(clip.updated_at),
        "video_url": media_url(assets["export"].id if "export" in assets else None, version),
        "preview_url": media_url(assets["preview"].id if "preview" in assets else None, version),
        "has_export": "export" in assets, "has_srt": "srt" in assets,
    }  # fmt: skip
    if detail:
        captions = [Caption.model_validate(c) for c in clip.captions or []]
        out["caption_pages"] = [
            {"index": p.index, "text": p.text, "start_ms": p.start_ms, "end_ms": p.end_ms, "measured": p.measured}
            for p in pages_of(captions)
        ]
        latest = sorted(clip.feedback, key=lambda f: f.created_at)
        out["feedback"] = [
            {"decision": f.decision, "rejection_reason": f.rejection_reason, "rating": f.rating,
             "social_metrics": f.social_metrics, "note": f.note, "created_at": iso(f.created_at)}  # fmt: skip
            for f in latest
        ]
    return out


def project_out(s: Session, project: Project, *, detail: bool = False) -> dict:
    jobs = sorted(project.jobs, key=lambda j: j.created_at)
    pipeline = next((j for j in reversed(jobs) if j.kind == "pipeline"), None)
    out = {
        "id": project.id, "name": project.name, "settings": project.settings, "summary": project.summary,
        "is_test": project.is_test, "created_at": iso(project.created_at), "source": source_out(s, project.source),
        "status": pipeline.status if pipeline else "none", "job_id": pipeline.id if pipeline else None,
        "clip_count": len(project.clips),
        "current_stage": next((st.name for st in pipeline.stages if st.status == "running"), None) if pipeline else None,
    }  # fmt: skip
    if detail:
        out["job"] = job_out(s, pipeline) if pipeline else None
        out["clips"] = [clip_out(s, c) for c in sorted(project.clips, key=lambda c: c.rank)]
        out["render_jobs"] = [job_out(s, j) for j in jobs if j.kind == "render" and j.status in ("queued", "running")]
        out["candidates"] = [
            {"id": c.id, "start_ms": c.start_ms, "end_ms": c.end_ms, "origins": c.origins, "title": c.title,
             "event_type": c.event_type, "final_score": c.final_score, "heuristic_score": c.heuristic_score,
             "multimodal_score": c.multimodal_score, "editorial_score": c.editorial_score, "penalties": c.penalties,
             "status": c.status, "reject_reason": c.reject_reason, "justification": c.justification}  # fmt: skip
            for c in sorted(project.candidates, key=lambda c: c.final_score or 0, reverse=True)
        ]
    return out
