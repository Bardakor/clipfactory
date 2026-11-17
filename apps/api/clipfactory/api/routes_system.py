"""Health, statistics, settings, model routing, media serving, storage and publishing status."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from clipfactory.ai.registry import BUDGETS, REQUIRED_MODALITY, ModelRegistry
from clipfactory.analysis.ranking import PROFILE_WEIGHTS
from clipfactory.api.serializers import iso
from clipfactory.config import RENDERER_DIR, get_settings
from clipfactory.db import get_session
from clipfactory.domain import AI_TASKS, REJECTION_REASONS
from clipfactory.errors import NotFound
from clipfactory.live.watcher import HEARTBEAT_KEY
from clipfactory.models import AppSetting, Clip, InferenceUsage, Job, MediaAsset, Project, Source
from clipfactory.publishing.tiktok import tiktok_status
from clipfactory.settings_store import RuntimeSettings, load_runtime, save_runtime
from clipfactory.timeutil import now

router = APIRouter()


def _version(cmd: list[str]) -> str | None:
    if shutil.which(cmd[0]) is None:
        return None
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False)
        return (out.stdout or out.stderr).strip().splitlines()[0][:80]
    except (OSError, subprocess.TimeoutExpired, IndexError):
        return None


def process_alive(s: Session, heartbeat_key: str) -> bool:
    """True when the background process writing this heartbeat did so in the last 20 seconds."""
    beat = s.get(AppSetting, heartbeat_key)
    return bool(beat and now() - beat.updated_at < timedelta(seconds=20))


@router.get("/health")
def health(s: Session = Depends(get_session)) -> dict:
    settings = get_settings()
    deps = {
        "ffmpeg": _version(["ffmpeg", "-version"]),
        "ffprobe": _version(["ffprobe", "-version"]),
        "node": _version(["node", "--version"]),
        "yt_dlp": "installed" if importlib.util.find_spec("yt_dlp") else None,
        "streamlink": "installed" if importlib.util.find_spec("streamlink") else None,
        "mlx_whisper": "installed" if importlib.util.find_spec("mlx_whisper") else None,
        "remotion": "installed" if (RENDERER_DIR / "node_modules" / "remotion").exists() else None,
    }
    return {
        "ok": all(deps.values()),
        "dependencies": deps,
        "worker_alive": process_alive(s, "worker_heartbeat"),
        "live_alive": process_alive(s, HEARTBEAT_KEY),
        "openrouter_key_configured": settings.has_openrouter_key,  # a boolean only — the key itself never leaves the server
        "test_mode": settings.test_mode,
        "ai_adapter": settings.clipfactory_ai_adapter,
        "transcriber_override": settings.clipfactory_transcriber,
    }


@router.get("/stats")
def stats(s: Session = Depends(get_session)) -> dict:
    """Operational numbers, all measured. Anything produced by a test adapter is counted separately."""
    real_jobs = select(Job).where(Job.kind == "pipeline", Job.is_test.is_(False)).subquery()
    by_status = dict(s.execute(select(real_jobs.c.status, func.count()).group_by(real_jobs.c.status)).all())
    finished = s.execute(
        select(Job.started_at, Job.finished_at, Source.duration_ms)
        .join(Project, Project.id == Job.project_id).join(Source, Source.id == Project.source_id)
        .where(Job.kind == "pipeline", Job.status == "succeeded", Job.is_test.is_(False), Job.started_at.is_not(None))
    ).all()  # fmt: skip
    durations = [(f - st).total_seconds() for st, f, _ in finished if f and st]
    factors = [d / 1000 / max(1.0, (f - st).total_seconds()) for st, f, d in finished if f and st and d]
    actual, estimated, requests = s.execute(
        select(func.sum(InferenceUsage.cost_usd_actual), func.sum(InferenceUsage.cost_usd_estimated), func.count())
        .where(InferenceUsage.status == "ok")
    ).one()  # fmt: skip
    clips = select(Clip).where(Clip.is_test.is_(False)).subquery()
    history = s.execute(
        select(Project.id, Project.name, Project.is_test, Project.created_at, Job.status, Job.started_at, Job.finished_at)
        .join(Job, Job.project_id == Project.id).where(Job.kind == "pipeline").order_by(Job.created_at.desc()).limit(12)
    ).all()  # fmt: skip
    return {
        "sources_processed": s.scalar(
            select(func.count(func.distinct(Project.source_id))).join(Job, Job.project_id == Project.id)
            .where(Job.kind == "pipeline", Job.status == "succeeded", Job.is_test.is_(False))
        ),  # fmt: skip
        "clips_rendered": s.scalar(select(func.count()).select_from(clips).where(clips.c.render_status == "rendered")),
        "clips_awaiting_review": s.scalar(
            select(func.count()).select_from(clips).where(clips.c.render_status == "rendered", clips.c.approval == "pending")
        ),
        "clips_approved": s.scalar(select(func.count()).select_from(clips).where(clips.c.approval == "approved")),
        "jobs_succeeded": by_status.get("succeeded", 0),
        "jobs_failed": by_status.get("failed", 0),
        "jobs_active": by_status.get("running", 0) + by_status.get("queued", 0),
        "inference": {"actual_usd": round(actual or 0, 4), "estimated_usd": round(estimated or 0, 4), "requests": requests},
        "avg_processing_s": round(sum(durations) / len(durations), 1) if durations else None,
        "avg_realtime_factor": round(sum(factors) / len(factors), 2) if factors else None,
        "test_projects": s.scalar(select(func.count()).select_from(Project).where(Project.is_test.is_(True))),
        "history": [
            {"project_id": pid, "name": name, "is_test": test, "created_at": iso(created), "status": status,
             "processing_s": round((f - st).total_seconds(), 1) if f and st else None}  # fmt: skip
            for pid, name, test, created, status, st, f in history
        ],
    }


@router.get("/settings")
def get_runtime_settings(s: Session = Depends(get_session)) -> dict:
    return {"settings": load_runtime(s).model_dump(mode="json"), "rejection_reasons": REJECTION_REASONS,
            "profile_weights_default": PROFILE_WEIGHTS, "required_modality": REQUIRED_MODALITY}  # fmt: skip


@router.put("/settings")
def put_runtime_settings(body: RuntimeSettings, s: Session = Depends(get_session)) -> dict:
    return {"settings": save_runtime(s, body).model_dump(mode="json")}


@router.get("/models")
def models(refresh: bool = False, s: Session = Depends(get_session)) -> dict:
    """The live OpenRouter catalogue (capabilities and prices) and the routing it produces per preset."""
    registry = ModelRegistry(get_settings())
    registry.load(force=refresh)
    runtime = load_runtime(s)
    return {
        "fetched_at": registry.fetched_at,
        "tasks": AI_TASKS,
        "routing": {preset: registry.routing(runtime, preset) for preset in BUDGETS},
        "budgets": {name: vars(b) for name, b in BUDGETS.items()},
        "models": sorted((m.to_public() for m in registry.models.values()), key=lambda m: m["id"]),
    }


@router.get("/media/{asset_id}")
def media(asset_id: str, s: Session = Depends(get_session)) -> FileResponse:
    asset = s.get(MediaAsset, asset_id)
    if asset is None:
        raise NotFound("media not found")
    path = get_settings().path(asset.path)
    if not path.exists():
        raise NotFound("media file is no longer on disk")
    return FileResponse(path, media_type=asset.mime, headers={"Cache-Control": "private, max-age=3600"})


def _dir_size(path) -> int:  # noqa: ANN001
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) if path.exists() else 0


CLEANABLE = {
    "work": ("work",),  # intermediate cuts; re-created on the next render
    "frames": ("cache", "frames"),  # keyframes sent to vision models
    "llm": ("cache", "llm"),  # cached model answers; clearing means paying for them again
    "previews": ("previews",),
}


@router.get("/storage")
def storage() -> dict:
    settings = get_settings()
    usage = {name: _dir_size(settings.path(name)) for name in ("sources", "audio", "transcripts", "events", "exports", "previews", "work", "cache", "live")}
    return {"data_dir": str(settings.data_dir), "bytes": usage, "total_bytes": sum(usage.values()), "cleanable": list(CLEANABLE)}


class CleanupRequest(BaseModel):
    targets: list[Literal["work", "frames", "llm", "previews"]]


@router.post("/storage/cleanup")
def cleanup(body: CleanupRequest, s: Session = Depends(get_session)) -> dict:
    settings = get_settings()
    freed = 0
    for target in body.targets:
        folder = settings.path(*CLEANABLE[target])
        freed += _dir_size(folder)
        shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir(parents=True, exist_ok=True)
        if target == "previews":
            for asset in s.scalars(select(MediaAsset).where(MediaAsset.kind == "preview")):
                s.delete(asset)
    return {"freed_bytes": freed}


@router.get("/publishing/tiktok")
def publishing_status() -> dict:
    return tiktok_status()
