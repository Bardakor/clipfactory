"""User-tunable runtime settings, persisted in SQLite and edited from the dashboard."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from clipfactory.domain import AITask, Preset, SubtitleOptions, SubtitleStyle
from clipfactory.models import AppSetting

DEFAULT_VOCABULARY = [
    "GTA", "GTA RP", "GTA Online", "FiveM", "LSPD", "BCSO", "EMS", "RP", "Los Santos",
    "Sandy Shores", "Paleto", "Vinewood", "staff", "wipe", "freekill", "HRP", "carkill",
]  # fmt: skip

# Extra spellings for one content profile, put in front of the user's vocabulary for that project only, so
# transcripts of other profiles keep their cache key. Names as published by Rockstar Games for GTA VI.
PROFILE_VOCABULARY: dict[str, list[str]] = {
    "gta6": [
        "GTA 6", "GTA VI", "Vice City", "Leonida", "Leonida Keys", "Grassrivers", "Port Gellhorn", "Ambrosia",
        "Mount Kalaga", "Jason", "Lucia", "Cal Hampton", "Boobie Ike", "Dre'Quan", "Real Dimez", "Raul Bautista",
        "Brian Heder",
    ],
}  # fmt: skip

# Applied after transcription, whole-word and case-insensitive. Keys are what
# Whisper tends to write; values are the spelling we want.
DEFAULT_CORRECTIONS = {
    "five m": "FiveM",
    "fivem": "FiveM",
    "five-m": "FiveM",
    "l.s.p.d.": "LSPD",
    "lspd": "LSPD",
    "gta": "GTA",
    "ems": "EMS",
}


class AudioDetectorSettings(BaseModel):
    silence_db: float = Field(default=-42.0, ge=-80, le=-10)
    min_silence_ms: int = Field(default=450, ge=100, le=5000)
    peak_z: float = Field(default=2.2, ge=0.5, le=8)  # novelty z-score that counts as an event
    group_gap_ms: int = Field(default=1500, ge=0, le=10000)  # merge events closer than this


class RuntimeSettings(BaseModel):
    preset: Preset = "balanced"
    custom_models: dict[AITask, str] = {}
    use_video_input: bool = True  # only ever applied when the routed model accepts video
    transcription_provider: Literal["mlx", "openrouter"] = "mlx"
    mlx_model: str = "mlx-community/whisper-large-v3-turbo"
    openrouter_stt_model: str = "openai/whisper-large-v3-turbo"
    vocabulary: list[str] = DEFAULT_VOCABULARY
    corrections: dict[str, str] = DEFAULT_CORRECTIONS
    audio: AudioDetectorSettings = AudioDetectorSettings()
    llm_concurrency: int = Field(default=4, ge=1, le=16)
    llm_timeout_s: int = Field(default=120, ge=10, le=600)
    llm_max_retries: int = Field(default=3, ge=0, le=8)
    render_concurrency: int = Field(default=1, ge=1, le=4)
    remotion_concurrency: int | None = Field(default=None, ge=1, le=16)  # None → Remotion default
    stage_timeout_s: int = Field(default=6 * 3600, ge=60)
    cost_warning_usd: float = Field(default=2.0, ge=0)
    cost_limit_usd: float | None = Field(default=None, ge=0)  # hard stop per job; None = no cap
    subtitle_style: SubtitleStyle = "viral"
    subtitle_options: SubtitleOptions = SubtitleOptions()
    profile_weights: dict[str, dict[str, float]] = {}  # per-profile overrides of the rubric weights
    export_dir: str | None = None  # extra copy of final exports; None = data/exports only


_KEY = "runtime"


def load_runtime(session: Session) -> RuntimeSettings:
    row = session.get(AppSetting, _KEY)
    return RuntimeSettings.model_validate(row.value) if row else RuntimeSettings()


def save_runtime(session: Session, value: RuntimeSettings) -> RuntimeSettings:
    row = session.get(AppSetting, _KEY)
    data = value.model_dump(mode="json")
    if row:
        row.value = data
    else:
        session.add(AppSetting(key=_KEY, value=data))
    return value


def with_profile_vocabulary(runtime: RuntimeSettings, profile: str) -> RuntimeSettings:
    extra = PROFILE_VOCABULARY.get(profile)
    return runtime.model_copy(update={"vocabulary": extra + runtime.vocabulary}) if extra else runtime
