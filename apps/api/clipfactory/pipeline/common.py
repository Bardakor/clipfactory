"""Helpers shared by pipeline stages: loading inputs and wiring the analyst to usage accounting."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from clipfactory.ai.analyst import Analyst, FakeAnalyst, OpenRouterAnalyst
from clipfactory.ai.gateway import OpenRouterGateway, UsageRecord
from clipfactory.ai.registry import ModelRegistry
from clipfactory.analysis.audio import AudioFeatures
from clipfactory.analysis.visual import VisualFeatures
from clipfactory.config import Settings
from clipfactory.db import session_scope
from clipfactory.domain import ProjectSettings, Segment
from clipfactory.errors import NotFound
from clipfactory.models import AnalysisRun, Clip, InferenceUsage, MediaAsset, Project, Source
from clipfactory.pipeline.context import JobContext
from clipfactory.transcription.service import latest_transcript, segments_of


@dataclass
class Inputs:
    """A detached snapshot of what a stage needs, so no DB transaction stays open during long work."""

    project_id: str
    project_name: str
    settings: ProjectSettings
    source_id: str
    source_kind: str
    source_status: str
    source_path: Path | None
    origin_url: str | None
    title: str
    fingerprint: str | None
    duration_ms: int
    width: int
    height: int
    has_audio: bool
    facecam: dict | None


def load_inputs(ctx: JobContext) -> Inputs:
    with session_scope() as s:
        project = s.get(Project, ctx.project_id)
        if project is None:
            raise NotFound("project no longer exists")
        src = project.source
        return Inputs(
            project_id=project.id, project_name=project.name, settings=ProjectSettings.model_validate(project.settings),
            source_id=src.id, source_kind=src.kind, source_status=src.status,
            source_path=ctx.settings.path(src.path) if src.path else None, origin_url=src.origin_url, title=src.title,
            fingerprint=src.fingerprint, duration_ms=src.duration_ms or 0, width=src.width or 0, height=src.height or 0,
            has_audio=src.audio_codec is not None, facecam=src.facecam,
        )  # fmt: skip


def audio_path(settings: Settings, source_id: str) -> Path:
    return settings.path("audio", f"{source_id}.wav")


def events_dir(settings: Settings, source_id: str) -> Path:
    return settings.path("events", source_id)


def load_segments(s: Session, source_id: str) -> tuple[list[Segment], bool]:
    """(segments, is_test) of the source's most recent transcript."""
    transcript = latest_transcript(s, source_id)
    if transcript is None:
        return [], False
    return segments_of(transcript), transcript.is_test


def load_features(settings: Settings, source_id: str) -> tuple[AudioFeatures | None, VisualFeatures | None]:
    folder = events_dir(settings, source_id)
    audio = sorted(folder.glob("audio_*.npz"), key=lambda p: p.stat().st_mtime)
    visual = folder / "visual.npz"
    return (AudioFeatures.load(audio[-1]) if audio else None, VisualFeatures.load(visual) if visual.exists() else None)


def build_analyst(ctx: JobContext) -> tuple[Analyst | None, OpenRouterGateway | None]:
    """The analyst for this job, or (None, None) when no inference is configured.

    Running without a key is a supported, clearly labelled degraded mode
    (heuristic ranking only) — never a silent substitute for AI analysis.
    """
    if ctx.settings.clipfactory_ai_adapter == "fake":
        ctx.log("ADAPTATEUR DE TEST actif : aucune inférence réelle n'est effectuée")
        return FakeAnalyst(), None
    if not ctx.settings.has_openrouter_key:
        ctx.warn("OPENROUTER_API_KEY absente : analyse IA ignorée, classement heuristique uniquement")
        return None, None

    def on_usage(record: UsageRecord) -> None:
        with session_scope() as s:
            s.add(InferenceUsage(
                project_id=ctx.project_id, job_id=ctx.job_id, task=record.task, model=record.model, status=record.status,
                prompt_tokens=record.prompt_tokens, completion_tokens=record.completion_tokens,
                cost_usd_actual=record.cost_usd_actual, cost_usd_estimated=record.cost_usd_estimated,
                latency_ms=record.latency_ms, generation_id=record.generation_id, error=record.error,
            ))  # fmt: skip
            if record.status in ("ok", "cached"):
                s.add(AnalysisRun(
                    project_id=ctx.project_id, job_id=ctx.job_id, task=record.task, model=record.model,
                    prompt_version=record.prompt_version, input_fingerprint=record.input_fingerprint,
                    status="succeeded", cached=record.status == "cached",
                ))  # fmt: skip

    registry = ModelRegistry(ctx.settings)
    gateway = OpenRouterGateway(
        ctx.settings, registry, concurrency=ctx.runtime.llm_concurrency, timeout_s=ctx.runtime.llm_timeout_s,
        max_retries=ctx.runtime.llm_max_retries, cost_warning_usd=ctx.runtime.cost_warning_usd,
        cost_limit_usd=ctx.runtime.cost_limit_usd, on_usage=on_usage, on_warning=ctx.warn,
    )  # fmt: skip
    return OpenRouterAnalyst(gateway, registry, ctx.runtime, on_warning=ctx.warn), gateway


def purge_clip_files(settings: Settings, s: Session, clip: Clip) -> None:
    """Delete every file derived from a clip (work dir, preview, exports) and their asset rows."""
    shutil.rmtree(settings.path("work", clip.id), ignore_errors=True)
    for asset in s.scalars(select(MediaAsset).where(MediaAsset.clip_id == clip.id)):
        settings.path(asset.path).unlink(missing_ok=True)
        s.delete(asset)


def replace_assets(s: Session, settings: Settings, clip_id: str, files: dict[str, Path]) -> dict[str, str]:
    """Register published files as the clip's current assets of their kind. Returns kind → asset id."""
    mimes = {"export": "video/mp4", "preview": "video/mp4", "srt": "application/x-subrip", "json": "application/json"}
    ids: dict[str, str] = {}
    for kind, path in files.items():
        for old in s.scalars(select(MediaAsset).where(MediaAsset.clip_id == clip_id, MediaAsset.kind == kind)):
            if settings.path(old.path) != path:
                settings.path(old.path).unlink(missing_ok=True)
            s.delete(old)
        asset = MediaAsset(clip_id=clip_id, kind=kind, path=settings.rel(path), mime=mimes[kind], bytes=path.stat().st_size)
        s.add(asset)
        s.flush()
        ids[kind] = asset.id
    return ids


def source_of(s: Session, project_id: str) -> Source:
    project = s.get(Project, project_id)
    if project is None:
        raise NotFound("project no longer exists")
    return project.source
