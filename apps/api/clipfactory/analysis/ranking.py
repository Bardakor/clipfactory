"""ClipRanker: hybrid editorial scoring, de-duplication and diverse selection.

The 0–100 output is an *editorial estimate* of short-form potential built from
observable qualities. It is not a probability of going viral and is never
presented as one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from clipfactory.analysis.candidates import length_fit
from clipfactory.timeutil import clamp, iou

RUBRIC = ("hook", "comprehensibility", "surprise", "humor", "emotion", "narrative", "pacing", "payoff", "originality")

# What each content profile cares about. Weights are relative; they are normalised at use.
PROFILE_WEIGHTS: dict[str, dict[str, float]] = {
    "gta_rp": {"hook": 1.2, "comprehensibility": 1.3, "surprise": 1.0, "humor": 1.8, "emotion": 0.9,
               "narrative": 1.5, "pacing": 0.8, "payoff": 1.3, "originality": 0.9},
    "gta_action": {"hook": 1.5, "comprehensibility": 1.0, "surprise": 1.5, "humor": 0.8, "emotion": 1.4,
                   "narrative": 0.8, "pacing": 1.3, "payoff": 1.3, "originality": 0.9},
    "general": {k: 1.0 for k in RUBRIC},
}  # fmt: skip
PROFILE_WEIGHTS["custom"] = PROFILE_WEIGHTS["general"]

PROFILE_FOCUS = {
    "gta_rp": "GTA roleplay: funny dialogue, absurd interactions, RP misunderstandings, character conflict, "
              "unexpected decisions, voice reactions, funny consequences, narrative coherence.",
    "gta_action": "GTA action: chases, shootouts, crashes, close calls, surprising gameplay, dramatic escapes, "
                  "strong reactions.",
    "general": "General gaming: balanced between funny moments, impressive plays and strong reactions.",
    "custom": "Follow the user's custom editorial instructions above everything else.",
}  # fmt: skip

# How the three evidence sources combine. Missing components are dropped and the rest renormalised.
COMPONENT_WEIGHTS = {"heuristic": 0.20, "multimodal": 0.25, "editorial": 0.55}

MIN_FEEDBACK_FOR_ADJUSTMENT = 10
MAX_FEEDBACK_ADJUSTMENT = 5.0


def rubric_weights(profile: str, overrides: dict[str, dict[str, float]] | None = None) -> dict[str, float]:
    weights = dict(PROFILE_WEIGHTS.get(profile, PROFILE_WEIGHTS["general"]))
    for key, value in ((overrides or {}).get(profile) or {}).items():
        if key in weights and value >= 0:
            weights[key] = float(value)
    total = sum(weights.values()) or 1.0
    return {k: v / total for k, v in weights.items()}


def editorial_from_rubric(rubric: dict[str, float], weights: dict[str, float]) -> float:
    """Weighted mean of 0–10 rubric marks → 0–1."""
    return round(clamp(sum(weights[k] * clamp(float(rubric.get(k, 0)), 0, 10) for k in RUBRIC) / 10.0, 0, 1), 4)


def multimodal_from_visual(visual: dict) -> float:
    """0–1 from a validated visual analysis: clarity and intensity, discounted by the model's own confidence."""
    clarity = clamp(float(visual.get("visual_clarity", 0)) / 10, 0, 1)
    action = clamp(float(visual.get("action_intensity", 0)) / 10, 0, 1)
    interest = clamp(float(visual.get("entertainment_value", 0)) / 10, 0, 1)
    confidence = clamp(float(visual.get("confidence", 0.5)), 0, 1)
    return round((0.3 * clarity + 0.25 * action + 0.45 * interest) * (0.5 + 0.5 * confidence), 4)


@dataclass
class ScoreInput:
    candidate_id: str
    start_ms: int
    end_ms: int
    features: dict[str, float]
    heuristic: float
    multimodal: float | None = None
    editorial: float | None = None
    missing_context: float = 0.0  # 0–1 severity, from the editorial model
    confidence: float | None = None  # lowest confidence among the AI evidence
    transcript_confidence: float | None = None
    event_type: str = "unknown"
    text: str = ""


@dataclass
class ScoreResult:
    candidate_id: str
    score: float  # 0–100
    components: dict[str, float]
    penalties: dict[str, float]
    basis: str  # ai | heuristic
    extra: dict = field(default_factory=dict)


def feedback_adjustments(history: list[tuple[str, str]]) -> dict[str, float]:
    """Transparent per-event-type nudges learned from review decisions.

    ``history`` is (event_type, decision). With fewer than ten decisions this
    returns nothing at all: a handful of examples is not a model. With more, an
    event type the user accepts more (or less) often than their overall average
    moves by at most ±5 points, shrunk towards zero by Laplace smoothing.
    """
    decided = [(t, d) for t, d in history if d in ("accepted", "rejected")]
    if len(decided) < MIN_FEEDBACK_FOR_ADJUSTMENT:
        return {}
    overall = sum(1 for _, d in decided if d == "accepted") / len(decided)
    out: dict[str, float] = {}
    for event_type in {t for t, _ in decided}:
        own = [d for t, d in decided if t == event_type]
        if len(own) < 3:
            continue
        rate = (sum(1 for d in own if d == "accepted") + 2 * overall) / (len(own) + 2)
        out[event_type] = round(clamp((rate - overall) * 2 * MAX_FEEDBACK_ADJUSTMENT, -MAX_FEEDBACK_ADJUSTMENT, MAX_FEEDBACK_ADJUSTMENT), 2)
    return out


def score_candidate(item: ScoreInput, adjustments: dict[str, float] | None = None) -> ScoreResult:
    available = {"heuristic": item.heuristic}
    if item.multimodal is not None:
        available["multimodal"] = item.multimodal
    if item.editorial is not None:
        available["editorial"] = item.editorial
    total_weight = sum(COMPONENT_WEIGHTS[k] for k in available)
    base = sum(COMPONENT_WEIGHTS[k] * v for k, v in available.items()) / total_weight

    duration_s = (item.end_ms - item.start_ms) / 1000
    penalties = {
        "missing_context": round(0.18 * clamp(item.missing_context, 0, 1), 4),
        "dead_time": round(0.15 * clamp(item.features.get("dead_time", 0), 0, 1), 4),
        "length": round(0.10 * (1 - length_fit(duration_s)), 4),
        "low_confidence": round(0.10 * (1 - clamp(item.confidence, 0, 1)), 4) if item.confidence is not None else 0.0,
        "unclear_audio": round(0.06 * (1 - clamp(item.transcript_confidence, 0, 1)), 4)
        if item.transcript_confidence is not None else 0.0,
    }  # fmt: skip
    score = 100 * clamp(base - sum(penalties.values()), 0, 1)
    adjustment = (adjustments or {}).get(item.event_type, 0.0)
    return ScoreResult(
        candidate_id=item.candidate_id,
        score=round(clamp(score + adjustment, 0, 100), 1),
        components={k: round(v, 4) for k, v in available.items()},
        penalties={k: v for k, v in penalties.items() if v > 0},
        basis="ai" if item.editorial is not None else "heuristic",
        extra={"feedback_adjustment": adjustment} if adjustment else {},
    )


_TOKEN = re.compile(r"[\wÀ-ÿ']+")


def text_similarity(a: str, b: str) -> float:
    """Jaccard overlap of word sets — catches the same exchange cut two ways."""
    ta, tb = set(_TOKEN.findall(a.lower())), set(_TOKEN.findall(b.lower()))
    if len(ta) < 4 or len(tb) < 4:
        return 0.0
    return len(ta & tb) / len(ta | tb)


@dataclass
class Selection:
    selected: list[str]
    rejected: dict[str, str]  # candidate id → reason
    explanation: str


def select_clips(
    scored: list[tuple[ScoreInput, ScoreResult]],
    *,
    count: int,
    threshold: float,
    max_overlap: float = 0.25,
    max_text_similarity: float = 0.6,
    repeat_penalty: float = 3.0,
) -> Selection:
    """Greedy, overlap-aware, diversity-seeking selection.

    Returns fewer than ``count`` clips when the source does not contain enough
    material above the quality threshold — and says so.
    """
    selected: list[tuple[ScoreInput, ScoreResult]] = []
    rejected: dict[str, str] = {}
    remaining = sorted(scored, key=lambda p: p[1].score, reverse=True)
    type_counts: dict[str, int] = {}
    while remaining and len(selected) < count:
        # Re-rank each round: another clip of an already-chosen event type is worth a little less.
        def effective(pair: tuple[ScoreInput, ScoreResult]) -> float:
            return pair[1].score - repeat_penalty * type_counts.get(pair[0].event_type, 0)

        remaining.sort(key=effective, reverse=True)
        item, result = remaining.pop(0)
        if result.score < threshold:
            rejected[item.candidate_id] = f"below the quality threshold ({result.score:.0f} < {threshold:.0f})"
            continue
        clash = next(
            (
                s for s, _ in selected
                if iou(s.start_ms, s.end_ms, item.start_ms, item.end_ms) > max_overlap
                or text_similarity(s.text, item.text) > max_text_similarity
            ),
            None,
        )  # fmt: skip
        if clash is not None:
            rejected[item.candidate_id] = "duplicate of a higher-ranked clip"
            continue
        selected.append((item, result))
        type_counts[item.event_type] = type_counts.get(item.event_type, 0) + 1
    for item, result in remaining:
        rejected.setdefault(
            item.candidate_id,
            "not needed: requested number of clips reached" if result.score >= threshold
            else f"below the quality threshold ({result.score:.0f} < {threshold:.0f})",
        )  # fmt: skip

    below = sum(1 for r in rejected.values() if r.startswith("below"))
    duplicates = sum(1 for r in rejected.values() if r.startswith("duplicate"))
    if len(selected) >= count:
        explanation = f"{len(selected)} clips selected from {len(scored)} evaluated candidates."
    else:
        explanation = (
            f"Only {len(selected)} of the {count} requested clips met the bar: of {len(scored)} candidates "
            f"evaluated, {below} scored under the quality threshold ({threshold:.0f}) and {duplicates} "
            "duplicated a stronger clip. Lower the threshold or use a longer source to get more."
        )
    return Selection([i.candidate_id for i, _ in selected], rejected, explanation)
