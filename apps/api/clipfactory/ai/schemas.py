"""Structured outputs expected from language models. Everything is validated before use.

Fields have no defaults on purpose: strict structured-output modes require every
property to be present, and a missing field should be a validation error rather
than a silently invented value.
"""

from __future__ import annotations

from pydantic import BaseModel, field_validator


def _clamp10(v: float) -> float:
    return max(0.0, min(10.0, float(v)))


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, float(v)))


class Moment(BaseModel):
    start_s: float
    end_s: float
    title: str
    event_type: str
    setup: str
    payoff: str
    why_entertaining: str
    confidence: float

    _c = field_validator("confidence")(_clamp01)


class MomentList(BaseModel):
    moments: list[Moment]


class VisualAnalysis(BaseModel):
    event_type: str
    participants: list[str]
    trigger: str
    outcome: str
    why_entertaining: str
    observations: list[str]
    uncertainties: list[str]
    supports_transcript: bool
    visual_clarity: float
    action_intensity: float
    entertainment_value: float
    confidence: float

    _c10 = field_validator("visual_clarity", "action_intensity", "entertainment_value")(_clamp10)
    _c01 = field_validator("confidence")(_clamp01)


class EditorialScore(BaseModel):
    candidate_id: str
    main_event: str
    event_type: str
    hook: float
    comprehensibility: float
    surprise: float
    humor: float
    emotion: float
    narrative: float
    pacing: float
    payoff: float
    originality: float
    missing_context: float
    confidence: float
    justification: str

    _c10 = field_validator(
        "hook", "comprehensibility", "surprise", "humor", "emotion", "narrative", "pacing", "payoff", "originality"
    )(_clamp10)
    _c01 = field_validator("missing_context", "confidence")(_clamp01)


class EditorialScores(BaseModel):
    scores: list[EditorialScore]


class Critique(BaseModel):
    candidate_id: str
    verdict: str  # keep | adjust | reject
    issue: str
    start_offset_s: float  # seconds to add to the clip start (negative = start earlier)
    end_offset_s: float

    @field_validator("verdict")
    @classmethod
    def _verdict(cls, v: str) -> str:
        v = v.strip().lower()
        return v if v in {"keep", "adjust", "reject"} else "keep"


class Critiques(BaseModel):
    critiques: list[Critique]


class SocialCopy(BaseModel):
    candidate_id: str
    title: str
    caption: str
    hashtags: list[str]

    @field_validator("hashtags")
    @classmethod
    def _tags(cls, v: list[str]) -> list[str]:
        cleaned = []
        for tag in v:
            tag = "#" + "".join(ch for ch in tag.lstrip("#") if ch.isalnum() or ch == "_")
            if len(tag) > 1 and tag not in cleaned:
                cleaned.append(tag)
        return cleaned[:8]


class SocialCopies(BaseModel):
    items: list[SocialCopy]
