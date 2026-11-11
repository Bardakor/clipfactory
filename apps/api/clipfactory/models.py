"""Database schema. Media files live on disk; rows only reference them by relative path.

All media timestamps are integer milliseconds on the *source* timeline unless a
column name says otherwise (clip captions are clip-relative).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from clipfactory.db import Base
from clipfactory.timeutil import now


def new_id() -> str:
    return uuid.uuid4().hex


def _pk() -> Mapped[str]:
    return mapped_column(String(32), primary_key=True, default=new_id)


def _fk(target: str, *, nullable: bool = False) -> Mapped[Any]:
    return mapped_column(String(32), ForeignKey(target, ondelete="CASCADE"), nullable=nullable, index=True)


def _created() -> Mapped[datetime]:
    return mapped_column(default=now)


class Source(Base):
    """An original video, kept untouched and separate from everything derived from it."""

    __tablename__ = "sources"
    id: Mapped[str] = _pk()
    kind: Mapped[str] = mapped_column(String(16))  # upload | youtube
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | ready | failed
    title: Mapped[str] = mapped_column(String(300), default="")
    channel: Mapped[str | None] = mapped_column(String(200))
    origin_url: Mapped[str | None] = mapped_column(String(500))
    original_filename: Mapped[str | None] = mapped_column(String(300))
    rights_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    path: Mapped[str | None] = mapped_column(String(500))  # relative to data dir
    fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)
    bytes: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    fps: Mapped[float | None] = mapped_column(Float)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    video_codec: Mapped[str | None] = mapped_column(String(40))
    audio_codec: Mapped[str | None] = mapped_column(String(40))
    audio_channels: Mapped[int | None] = mapped_column(Integer)
    probe: Mapped[dict | None] = mapped_column(JSON)  # full ffprobe track metadata
    facecam: Mapped[dict | None] = mapped_column(JSON)  # detected region, or {"found": false}
    error: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = _created()

    projects: Mapped[list[Project]] = relationship(back_populates="source", cascade="all, delete-orphan")
    assets: Mapped[list[MediaAsset]] = relationship(back_populates="source", cascade="all, delete-orphan")


class MediaAsset(Base):
    """Any derived or exported file: audio, thumbnail, cut, preview, export, srt, json."""

    __tablename__ = "media_assets"
    id: Mapped[str] = _pk()
    source_id: Mapped[str | None] = _fk("sources.id", nullable=True)
    clip_id: Mapped[str | None] = _fk("clips.id", nullable=True)
    kind: Mapped[str] = mapped_column(String(24), index=True)
    path: Mapped[str] = mapped_column(String(500))
    mime: Mapped[str] = mapped_column(String(60), default="application/octet-stream")
    bytes: Mapped[int] = mapped_column(Integer, default=0)
    meta: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = _created()

    source: Mapped[Source | None] = relationship(back_populates="assets")


class Project(Base):
    """One clipping run: a source plus the settings chosen for it."""

    __tablename__ = "projects"
    id: Mapped[str] = _pk()
    source_id: Mapped[str] = _fk("sources.id")
    name: Mapped[str] = mapped_column(String(300))
    settings: Mapped[dict] = mapped_column(JSON)
    summary: Mapped[dict | None] = mapped_column(JSON)  # selection outcome + explanations
    is_test: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = _created()

    source: Mapped[Source] = relationship(back_populates="projects")
    jobs: Mapped[list[Job]] = relationship(back_populates="project", cascade="all, delete-orphan")
    clips: Mapped[list[Clip]] = relationship(back_populates="project", cascade="all, delete-orphan")
    candidates: Mapped[list[Candidate]] = relationship(back_populates="project", cascade="all, delete-orphan")


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = _pk()
    project_id: Mapped[str] = _fk("projects.id")
    clip_id: Mapped[str | None] = mapped_column(String(32), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # pipeline | render
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    payload: Mapped[dict | None] = mapped_column(JSON)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    worker_id: Mapped[str | None] = mapped_column(String(64))
    heartbeat_at: Mapped[datetime | None] = mapped_column()
    error: Mapped[dict | None] = mapped_column(JSON)
    warnings: Mapped[list | None] = mapped_column(JSON)
    is_test: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = _created()
    started_at: Mapped[datetime | None] = mapped_column()
    finished_at: Mapped[datetime | None] = mapped_column()

    project: Mapped[Project] = relationship(back_populates="jobs")
    stages: Mapped[list[JobStage]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="JobStage.position"
    )


class JobStage(Base):
    __tablename__ = "job_stages"
    __table_args__ = (UniqueConstraint("job_id", "name"),)
    id: Mapped[str] = _pk()
    job_id: Mapped[str] = _fk("jobs.id")
    name: Mapped[str] = mapped_column(String(32))
    position: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    progress: Mapped[float | None] = mapped_column(Float)  # null = not measurable
    detail: Mapped[str | None] = mapped_column(String(300))
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    metrics: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[dict | None] = mapped_column(JSON)
    started_at: Mapped[datetime | None] = mapped_column()
    finished_at: Mapped[datetime | None] = mapped_column()
    duration_ms: Mapped[int | None] = mapped_column(Integer)

    job: Mapped[Job] = relationship(back_populates="stages")


class JobLog(Base):
    __tablename__ = "job_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = _fk("jobs.id")
    stage: Mapped[str | None] = mapped_column(String(32))
    level: Mapped[str] = mapped_column(String(8), default="info")
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict | None] = mapped_column(JSON)
    ts: Mapped[datetime] = _created()


class Transcript(Base):
    __tablename__ = "transcripts"
    id: Mapped[str] = _pk()
    source_id: Mapped[str] = _fk("sources.id")
    cache_key: Mapped[str] = mapped_column(String(64), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(120))
    language: Mapped[str | None] = mapped_column(String(8))
    precision: Mapped[str] = mapped_column(String(8), default="segment")  # word | segment
    edited: Mapped[bool] = mapped_column(Boolean, default=False)
    is_test: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = _created()

    segments: Mapped[list[TranscriptSegment]] = relationship(
        back_populates="transcript", cascade="all, delete-orphan", order_by="TranscriptSegment.start_ms"
    )


class TranscriptSegment(Base):
    __tablename__ = "transcript_segments"
    __table_args__ = (Index("ix_segments_transcript_start", "transcript_id", "start_ms"),)
    id: Mapped[str] = _pk()
    transcript_id: Mapped[str] = _fk("transcripts.id")
    start_ms: Mapped[int] = mapped_column(Integer)
    end_ms: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(String(8))
    speaker_id: Mapped[str | None] = mapped_column(String(16))
    confidence: Mapped[float | None] = mapped_column(Float)
    words: Mapped[list | None] = mapped_column(JSON)  # [{w, s, e, p}] absolute ms; null = no word timing
    provenance: Mapped[str] = mapped_column(String(120))

    transcript: Mapped[Transcript] = relationship(back_populates="segments")


class AudioEvent(Base):
    __tablename__ = "audio_events"
    id: Mapped[str] = _pk()
    source_id: Mapped[str] = _fk("sources.id")
    cache_key: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    start_ms: Mapped[int] = mapped_column(Integer)
    end_ms: Mapped[int] = mapped_column(Integer)
    peak_ms: Mapped[int] = mapped_column(Integer)
    score: Mapped[float] = mapped_column(Float)  # normalised 0–1 confidence
    meta: Mapped[dict | None] = mapped_column(JSON)


class VisualEvent(Base):
    __tablename__ = "visual_events"
    id: Mapped[str] = _pk()
    source_id: Mapped[str] = _fk("sources.id")
    candidate_id: Mapped[str | None] = mapped_column(String(32), index=True)
    cache_key: Mapped[str | None] = mapped_column(String(64), index=True)
    origin: Mapped[str] = mapped_column(String(16))  # heuristic | model
    kind: Mapped[str] = mapped_column(String(48))
    start_ms: Mapped[int] = mapped_column(Integer)
    end_ms: Mapped[int] = mapped_column(Integer)
    confidence: Mapped[float | None] = mapped_column(Float)
    description: Mapped[dict | None] = mapped_column(JSON)


class Candidate(Base):
    __tablename__ = "candidates"
    id: Mapped[str] = _pk()
    project_id: Mapped[str] = _fk("projects.id")
    start_ms: Mapped[int] = mapped_column(Integer)
    end_ms: Mapped[int] = mapped_column(Integer)
    origins: Mapped[list] = mapped_column(JSON)  # e.g. ["transcript_llm", "signals"]
    title: Mapped[str] = mapped_column(String(300), default="")
    event_type: Mapped[str] = mapped_column(String(48), default="unknown")
    structure: Mapped[dict | None] = mapped_column(JSON)  # setup / payoff / why
    evidence: Mapped[dict | None] = mapped_column(JSON)  # signal stats, visual analysis, rubric
    heuristic_score: Mapped[float | None] = mapped_column(Float)  # 0–1
    multimodal_score: Mapped[float | None] = mapped_column(Float)  # 0–1
    editorial_score: Mapped[float | None] = mapped_column(Float)  # 0–1
    penalties: Mapped[dict | None] = mapped_column(JSON)
    final_score: Mapped[float | None] = mapped_column(Float)  # 0–100 editorial estimate
    justification: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="pool")  # pool | selected | rejected
    reject_reason: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = _created()

    project: Mapped[Project] = relationship(back_populates="candidates")


class AnalysisRun(Base):
    """One logical AI task over one input, with the model + prompt version that produced it."""

    __tablename__ = "analysis_runs"
    id: Mapped[str] = _pk()
    project_id: Mapped[str | None] = _fk("projects.id", nullable=True)
    job_id: Mapped[str | None] = mapped_column(String(32), index=True)
    task: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(120))
    prompt_version: Mapped[str] = mapped_column(String(32))
    input_fingerprint: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    cached: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = _created()


class InferenceUsage(Base):
    """One HTTP call to an inference provider."""

    __tablename__ = "inference_usage"
    id: Mapped[str] = _pk()
    project_id: Mapped[str | None] = mapped_column(String(32), index=True)
    job_id: Mapped[str | None] = mapped_column(String(32), index=True)
    task: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(24))
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd_actual: Mapped[float | None] = mapped_column(Float)  # usage.cost reported by OpenRouter
    cost_usd_estimated: Mapped[float | None] = mapped_column(Float)  # tokens × catalogue price
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    generation_id: Mapped[str | None] = mapped_column(String(80))
    error: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = _created()


class Clip(Base):
    """A selected candidate with everything needed to (re)render it."""

    __tablename__ = "clips"
    id: Mapped[str] = _pk()
    project_id: Mapped[str] = _fk("projects.id")
    candidate_id: Mapped[str | None] = mapped_column(String(32))
    rank: Mapped[int] = mapped_column(Integer, default=0)
    start_ms: Mapped[int] = mapped_column(Integer)
    end_ms: Mapped[int] = mapped_column(Integer)
    original_start_ms: Mapped[int] = mapped_column(Integer)
    original_end_ms: Mapped[int] = mapped_column(Integer)
    score: Mapped[float] = mapped_column(Float, default=0)
    score_basis: Mapped[str] = mapped_column(String(16), default="ai")  # ai | heuristic | test
    title: Mapped[str] = mapped_column(String(300), default="")
    main_event: Mapped[str] = mapped_column(String(300), default="")
    justification: Mapped[str] = mapped_column(Text, default="")
    layout: Mapped[str] = mapped_column(String(16), default="auto")
    layout_params: Mapped[dict | None] = mapped_column(JSON)  # manual crop etc.
    resolved_layout: Mapped[dict | None] = mapped_column(JSON)  # what was actually rendered
    subtitle_style: Mapped[str] = mapped_column(String(16), default="viral")
    subtitle_options: Mapped[dict | None] = mapped_column(JSON)
    captions: Mapped[list | None] = mapped_column(JSON)  # clip-relative Caption tokens
    captions_precision: Mapped[str] = mapped_column(String(8), default="segment")
    captions_edited: Mapped[bool] = mapped_column(Boolean, default=False)
    social: Mapped[dict | None] = mapped_column(JSON)  # title, caption, hashtags
    render_status: Mapped[str] = mapped_column(String(16), default="none")
    approval: Mapped[str] = mapped_column(String(16), default="pending")
    edited: Mapped[bool] = mapped_column(Boolean, default=False)
    validation: Mapped[dict | None] = mapped_column(JSON)
    is_test: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(default=now, onupdate=now)

    project: Mapped[Project] = relationship(back_populates="clips")
    renders: Mapped[list[Render]] = relationship(back_populates="clip", cascade="all, delete-orphan")
    feedback: Mapped[list[Feedback]] = relationship(back_populates="clip", cascade="all, delete-orphan")


class Render(Base):
    __tablename__ = "renders"
    id: Mapped[str] = _pk()
    clip_id: Mapped[str] = _fk("clips.id")
    job_id: Mapped[str | None] = mapped_column(String(32), index=True)
    mode: Mapped[str] = mapped_column(String(8))  # preview | final
    status: Mapped[str] = mapped_column(String(16), default="rendering")
    settings: Mapped[dict | None] = mapped_column(JSON)
    validation: Mapped[dict | None] = mapped_column(JSON)
    output_asset_id: Mapped[str | None] = mapped_column(String(32))
    duration_ms: Mapped[int | None] = mapped_column(Integer)  # wall-clock render time
    error: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = _created()

    clip: Mapped[Clip] = relationship(back_populates="renders")


class Feedback(Base):
    """Append-only review history — the future training set for a learned ranker."""

    __tablename__ = "feedback"
    id: Mapped[str] = _pk()
    clip_id: Mapped[str] = _fk("clips.id")
    decision: Mapped[str] = mapped_column(String(16))  # accepted | rejected | rated | metrics
    rejection_reason: Mapped[str | None] = mapped_column(String(32))
    rating: Mapped[int | None] = mapped_column(Integer)
    edited: Mapped[bool] = mapped_column(Boolean, default=False)
    profile: Mapped[str | None] = mapped_column(String(24))
    event_type: Mapped[str | None] = mapped_column(String(48))
    original_score: Mapped[float | None] = mapped_column(Float)
    final_start_ms: Mapped[int | None] = mapped_column(Integer)
    final_end_ms: Mapped[int | None] = mapped_column(Integer)
    layout: Mapped[str | None] = mapped_column(String(16))
    subtitle_style: Mapped[str | None] = mapped_column(String(16))
    social_metrics: Mapped[dict | None] = mapped_column(JSON)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()

    clip: Mapped[Clip] = relationship(back_populates="feedback")


class AppSetting(Base):
    """Runtime configuration (model routing, budgets, defaults), one JSON value per key."""

    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(default=now, onupdate=now)


class PublishingRecord(Base):
    __tablename__ = "publishing_records"
    id: Mapped[str] = _pk()
    clip_id: Mapped[str] = _fk("clips.id")
    platform: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(24))
    external_id: Mapped[str | None] = mapped_column(String(120))
    detail: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = _created()
