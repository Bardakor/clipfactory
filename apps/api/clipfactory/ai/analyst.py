"""The analyst turns evidence into editorial judgements: one real implementation backed by
OpenRouter, and one deterministic test double that is labelled as such everywhere it is used."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from clipfactory.ai import prompts
from clipfactory.ai.gateway import OpenRouterGateway, Part, image_part, text_part, video_part
from clipfactory.ai.registry import ModelRegistry
from clipfactory.ai.schemas import (
    Critique,
    Critiques,
    EditorialScore,
    EditorialScores,
    Moment,
    MomentList,
    SocialCopies,
    SocialCopy,
    VisualAnalysis,
)
from clipfactory.analysis.ranking import PROFILE_FOCUS
from clipfactory.errors import AIAuthError, AIBudgetExceeded, AIError, AIInsufficientCredits
from clipfactory.logs import get_logger
from clipfactory.settings_store import RuntimeSettings
from clipfactory.timeutil import clock

log = get_logger("analyst")

MAX_VIDEO_BYTES = 8 * 1024 * 1024
SCORE_BATCH = 6


@dataclass
class EditorialContext:
    profile: str
    instructions: str
    min_len_s: int
    max_len_s: int
    preset: str

    def brief(self) -> str:
        return prompts.editorial_brief(PROFILE_FOCUS.get(self.profile, PROFILE_FOCUS["general"]), self.instructions, self.min_len_s, self.max_len_s)


@dataclass
class ClipBrief:
    """Everything a text model is shown about one candidate."""

    candidate_id: str
    duration_s: float
    transcript: str  # timestamps relative to the clip start
    features: dict | None = None
    visual: dict | None = None
    moment: dict | None = None
    context_before: str = ""
    context_after: str = ""

    def render(self) -> str:
        blocks = [f"### candidate_id: {self.candidate_id}", f"duration: {self.duration_s:.1f}s"]
        if self.moment:
            blocks.append(f"proposed as: {json.dumps(self.moment, ensure_ascii=False)}")
        if self.features:
            blocks.append(f"measured signals: {json.dumps(self.features)}")
        if self.visual:
            blocks.append(f"visual analysis: {json.dumps(self.visual, ensure_ascii=False)}")
        if self.context_before:
            blocks.append(f"transcript just before the clip:\n{self.context_before}")
        blocks.append(f"clip transcript:\n{self.transcript}")
        if self.context_after:
            blocks.append(f"transcript just after the clip:\n{self.context_after}")
        return "\n".join(blocks)


@dataclass
class VisualInput:
    candidate_id: str
    start_ms: int
    end_ms: int
    frames: list[tuple[int, Path]]  # (absolute ms, jpeg path)
    video: Path | None
    transcript: str
    audio_notes: str


class Analyst(Protocol):
    is_test: bool

    def find_moments(self, chunks: list[tuple[int, int, str, str]], per_chunk: int, ctx: EditorialContext) -> list[Moment]: ...
    def describe_visual(self, items: list[VisualInput], ctx: EditorialContext) -> dict[str, VisualAnalysis]: ...
    def score(self, briefs: list[ClipBrief], ctx: EditorialContext) -> dict[str, EditorialScore]: ...
    def critique(self, briefs: list[ClipBrief], ctx: EditorialContext) -> dict[str, Critique]: ...
    def social(self, briefs: list[ClipBrief], ctx: EditorialContext) -> dict[str, SocialCopy]: ...


_FATAL = (AIAuthError, AIInsufficientCredits, AIBudgetExceeded)


class OpenRouterAnalyst:
    is_test = False

    def __init__(self, gateway: OpenRouterGateway, registry: ModelRegistry, runtime: RuntimeSettings, on_warning=lambda m: None) -> None:  # noqa: ANN001
        self._gw = gateway
        self._registry = registry
        self._runtime = runtime
        self._warn = on_warning
        self._pool = ThreadPoolExecutor(max_workers=runtime.llm_concurrency, thread_name_prefix="llm")

    def _parallel(self, fn, items: list, what: str) -> list:  # noqa: ANN001
        """Run independent requests concurrently. One failed request costs its own result, not the stage —
        unless the failure is about the account itself, which would fail everything."""

        def guarded(item):  # noqa: ANN001, ANN202
            try:
                return fn(item)
            except _FATAL:
                raise
            except AIError as exc:
                self._warn(f"{what}: one request failed and was skipped ({exc.code}: {exc.message[:120]})")
                return None

        return list(self._pool.map(guarded, items))

    def find_moments(self, chunks, per_chunk, ctx):  # noqa: ANN001, ANN201
        models = self._registry.resolve("transcript_analysis", self._runtime, ctx.preset)

        def one(chunk: tuple[int, int, str, str]) -> list[Moment]:
            start_ms, end_ms, transcript, notes = chunk
            user = [text_part(
                f"{ctx.brief()}\n\nThis section covers {clock(start_ms)} to {clock(end_ms)} of the video "
                f"({start_ms / 1000:.1f}s to {end_ms / 1000:.1f}s). Propose at most {per_chunk} moments.\n\n"
                f"Audio/visual signal notes for this section:\n{notes}\n\nTranscript:\n{transcript}"
            )]  # fmt: skip
            result = self._gw.complete_json(task="transcript_analysis", models=models, system=prompts.MOMENTS_SYSTEM,
                                            user=user, schema=MomentList, prompt_version=prompts.MOMENTS_VERSION)  # fmt: skip
            return result.moments

        return [m for group in self._parallel(one, chunks, "transcript analysis") if group for m in group]

    def describe_visual(self, items, ctx):  # noqa: ANN001, ANN201
        image_models = self._registry.resolve("vision_image", self._runtime, ctx.preset)
        try:
            video_models = self._registry.resolve("vision_video", self._runtime, ctx.preset)
        except AIError:
            video_models = []

        def one(item: VisualInput) -> tuple[str, VisualAnalysis] | None:
            header = (
                f"{ctx.brief()}\n\nExcerpt: {clock(item.start_ms)} to {clock(item.end_ms)} of the source "
                f"({(item.end_ms - item.start_ms) / 1000:.1f}s).\nAudio signal notes: {item.audio_notes}\n\n"
                f"Transcript (seconds from the start of the excerpt):\n{item.transcript}\n"
            )
            # Real video is only sent to a model whose catalogue entry lists video input, and only when small.
            if item.video is not None and video_models and item.video.stat().st_size <= MAX_VIDEO_BYTES:
                try:
                    user = [text_part(header + "\nThe excerpt follows as a video."), video_part(item.video)]
                    result = self._gw.complete_json(task="vision_video", models=video_models, system=prompts.VISUAL_SYSTEM,
                                                    user=user, schema=VisualAnalysis, prompt_version=prompts.VISUAL_VERSION)  # fmt: skip
                    return item.candidate_id, result
                except _FATAL:
                    raise
                except AIError as exc:
                    self._warn(f"video input failed ({exc.code}); falling back to keyframes")
            user: list[Part] = [text_part(header + f"\n{len(item.frames)} frames follow, each preceded by its time label.")]
            for ms, path in item.frames:
                user.append(text_part(f"Frame at +{(ms - item.start_ms) / 1000:.1f}s (source {clock(ms)}):"))
                user.append(image_part(path))
            result = self._gw.complete_json(task="vision_image", models=image_models, system=prompts.VISUAL_SYSTEM,
                                            user=user, schema=VisualAnalysis, prompt_version=prompts.VISUAL_VERSION)  # fmt: skip
            return item.candidate_id, result

        return dict(r for r in self._parallel(one, items, "visual analysis") if r)

    def _batched(self, task, system, version, schema, attr, briefs, ctx, what):  # noqa: ANN001, ANN202
        models = self._registry.resolve(task, self._runtime, ctx.preset)
        batches = [briefs[i : i + SCORE_BATCH] for i in range(0, len(briefs), SCORE_BATCH)]

        def one(batch: list[ClipBrief]) -> list:
            body = "\n\n".join(b.render() for b in batch)
            user = [text_part(f"{ctx.brief()}\n\n{len(batch)} clips follow.\n\n{body}")]
            return getattr(self._gw.complete_json(task=task, models=models, system=system, user=user, schema=schema,
                                                  prompt_version=version, max_tokens=6000), attr)  # fmt: skip

        known = {b.candidate_id for b in briefs}
        out = {}
        for group in self._parallel(one, batches, what):
            for item in group or []:
                if item.candidate_id in known:  # ids the model made up are discarded
                    out[item.candidate_id] = item
        return out

    def score(self, briefs, ctx):  # noqa: ANN001, ANN201
        return self._batched("ranking", prompts.RANKING_SYSTEM, prompts.RANKING_VERSION, EditorialScores, "scores", briefs, ctx, "editorial ranking")

    def critique(self, briefs, ctx):  # noqa: ANN001, ANN201
        return self._batched("critique", prompts.CRITIQUE_SYSTEM, prompts.CRITIQUE_VERSION, Critiques, "critiques", briefs, ctx, "final critique")

    def social(self, briefs, ctx):  # noqa: ANN001, ANN201
        return self._batched("social_caption", prompts.SOCIAL_SYSTEM, prompts.SOCIAL_VERSION, SocialCopies, "items", briefs, ctx, "social copy")


class FakeAnalyst:
    """TEST ADAPTER. Deterministic stand-in so the pipeline can be exercised without inference.

    It performs no analysis whatsoever: outputs are derived mechanically from
    positions and measured signal statistics, and every clip it touches is
    flagged ``is_test`` in the database and labelled TEST in the interface.
    """

    is_test = True

    def find_moments(self, chunks, per_chunk, ctx):  # noqa: ANN001, ANN201
        moments = []
        for start_ms, end_ms, _transcript, _notes in chunks:
            length = min(max(ctx.min_len_s, 18), ctx.max_len_s)
            t = start_ms / 1000 + 4
            while t + length <= end_ms / 1000 and len(moments) < 200:
                moments.append(Moment(start_s=t, end_s=t + length, title="[TEST] moment simulé", event_type="test_event",
                                      setup="[TEST]", payoff="[TEST]", why_entertaining="[TEST] sortie de l'adaptateur de test",
                                      confidence=0.5))  # fmt: skip
                t += length + 12
        return moments[: max(per_chunk, 1) * max(1, len(chunks))]

    def describe_visual(self, items, ctx):  # noqa: ANN001, ANN201
        return {
            i.candidate_id: VisualAnalysis(
                event_type="test_event", participants=[], trigger="[TEST]", outcome="[TEST]",
                why_entertaining="[TEST] aucune analyse visuelle réelle", observations=[f"{len(i.frames)} images reçues"],
                uncertainties=["adaptateur de test"], supports_transcript=True, visual_clarity=5, action_intensity=5,
                entertainment_value=5, confidence=0.5,
            )  # fmt: skip
            for i in items
        }

    def score(self, briefs, ctx):  # noqa: ANN001, ANN201
        out = {}
        for b in briefs:
            mark = round(4 + 5 * float((b.features or {}).get("peak", 0.5)), 1)
            out[b.candidate_id] = EditorialScore(
                candidate_id=b.candidate_id, main_event="[TEST] événement simulé", event_type="test_event",
                hook=mark, comprehensibility=mark, surprise=mark, humor=mark, emotion=mark, narrative=mark, pacing=mark,
                payoff=mark, originality=mark, missing_context=0.0, confidence=0.5,
                justification="[TEST] Note produite par l'adaptateur de test, sans analyse éditoriale réelle.",
            )  # fmt: skip
        return out

    def critique(self, briefs, ctx):  # noqa: ANN001, ANN201
        return {b.candidate_id: Critique(candidate_id=b.candidate_id, verdict="keep", issue="", start_offset_s=0, end_offset_s=0) for b in briefs}

    def social(self, briefs, ctx):  # noqa: ANN001, ANN201
        return {
            b.candidate_id: SocialCopy(candidate_id=b.candidate_id, title="[TEST] Titre simulé",
                                       caption="[TEST] Légende simulée par l'adaptateur de test.", hashtags=["#test"])  # fmt: skip
            for b in briefs
        }
