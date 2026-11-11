"""Typed inputs and outputs shared by pipeline stages, the API and the renderer."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

Profile = Literal["gta_rp", "gta_action", "general", "custom"]
Preset = Literal["fast", "balanced", "max_quality", "custom"]
SubtitleStyle = Literal["viral", "clean", "gaming"]
Layout = Literal["auto", "full", "crop", "split", "dynamic", "manual"]
AITask = Literal[
    "transcript_analysis", "vision_image", "vision_video", "ranking", "critique", "social_caption"
]
AI_TASKS: tuple[AITask, ...] = (
    "transcript_analysis",
    "vision_image",
    "vision_video",
    "ranking",
    "critique",
    "social_caption",
)

PIPELINE_STAGES = (
    "import",
    "extract_audio",
    "transcribe",
    "detect_events",
    "analyze",
    "rank",
    "refine",
    "render",
    "validate",
)
RENDER_STAGES = ("render", "validate")

REJECTION_REASONS = (
    "not_funny",
    "missing_context",
    "too_long",
    "wrong_moment",
    "bad_subtitles",
    "bad_crop",
    "duplicate",
    "boring",
)


class Rect(BaseModel):
    """A rectangle in normalised source coordinates (0–1)."""

    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    w: float = Field(gt=0, le=1)
    h: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def _inside(self) -> Rect:
        self.w = min(self.w, 1 - self.x)
        self.h = min(self.h, 1 - self.y)
        return self


class SubtitleOptions(BaseModel):
    """Per-clip overrides of a subtitle style. ``None`` means "use the style's own default"."""

    font: Literal["Montserrat", "Inter", "Bebas Neue", "Anton"] | None = None
    scale: float = Field(default=1.0, ge=0.6, le=1.5)  # multiplies the style's base size
    accent: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    position: Literal["auto", "top", "middle", "bottom"] = "auto"
    emphasis: bool = True  # highlight the spoken word (only ever applied to words with measured timing)


class ProjectSettings(BaseModel):
    profile: Profile = "gta_rp"
    clip_count: int = Field(default=8, ge=1, le=30)
    min_len_s: int = Field(default=15, ge=5, le=120)
    max_len_s: int = Field(default=90, ge=10, le=180)
    # 45: on the first real GTA RP session the strongest clips scored 57–61 and decent ones 43–45.
    quality_threshold: int = Field(default=45, ge=0, le=100)
    preset: Preset = "balanced"
    subtitle_style: SubtitleStyle = "viral"
    subtitle_options: SubtitleOptions = SubtitleOptions()
    burn_subtitles: bool = True
    layout: Layout = "auto"
    width: int = 1080
    height: int = 1920
    fps: Literal[30, 60] = 30
    language: Literal["auto", "fr", "en"] = "auto"
    instructions: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def _lengths(self) -> ProjectSettings:
        if self.max_len_s <= self.min_len_s:
            raise ValueError("max_len_s must be greater than min_len_s")
        if (self.width, self.height) not in {(1080, 1920), (720, 1280)}:
            raise ValueError("unsupported output resolution")
        return self


# ── media ───────────────────────────────────────────────────────────────────


class ProbeResult(BaseModel):
    duration_ms: int
    fps: float
    width: int
    height: int
    video_codec: str
    audio_codec: str | None
    audio_channels: int | None
    audio_sample_rate: int | None
    bit_rate: int | None
    tracks: list[dict]

    @property
    def has_audio(self) -> bool:
        return self.audio_codec is not None


# ── transcript ──────────────────────────────────────────────────────────────


class Word(BaseModel):
    w: str
    s: int  # absolute start ms
    e: int  # absolute end ms
    p: float | None = None  # probability, when the provider reports one


class Segment(BaseModel):
    start_ms: int
    end_ms: int
    text: str
    language: str | None = None
    speaker_id: str | None = None
    confidence: float | None = None
    words: list[Word] | None = None  # None → no word-level timing available
    provenance: str


class TranscriptResult(BaseModel):
    provider: str
    model: str
    language: str | None
    precision: Literal["word", "segment"]
    segments: list[Segment]
    is_test: bool = False
    cost_usd: float | None = None


# ── captions (clip-relative, matches @remotion/captions' Caption type) ──────


class Caption(BaseModel):
    text: str
    startMs: int
    endMs: int
    timestampMs: int | None = None
    confidence: float | None = None
    pageBreakAfter: bool | None = None


# ── layout ──────────────────────────────────────────────────────────────────


class Panel(BaseModel):
    """Draw ``src`` (normalised source rect) into ``dst`` (output pixels)."""

    src: Rect
    x: int
    y: int
    w: int
    h: int
    radius: int = 0


class LayoutSegment(BaseModel):
    from_ms: int
    to_ms: int
    panels: list[Panel]


class ResolvedLayout(BaseModel):
    requested: Layout
    used: Literal["full", "crop", "split", "manual", "dynamic"]
    reason: str
    segments: list[LayoutSegment]
    caption_y: int  # centre line of the caption block, output pixels
    caption_max_width: int


class ExportValidation(BaseModel):
    ok: bool
    checks: dict[str, bool]
    details: dict[str, object]
    failures: list[str]
