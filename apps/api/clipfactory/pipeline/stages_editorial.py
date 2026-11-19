"""Project-level stages: candidate discovery + multimodal analysis, ranking, boundary refinement."""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
from sqlalchemy import delete, select

from clipfactory.ai.analyst import ClipBrief, EditorialContext, VisualInput
from clipfactory.ai.registry import BUDGETS, AnalysisBudget
from clipfactory.analysis import candidates as cand
from clipfactory.analysis import ranking
from clipfactory.analysis.audio import silence_intervals, speech_stats
from clipfactory.analysis.refine import refine_boundaries
from clipfactory.db import session_scope
from clipfactory.domain import Segment
from clipfactory.errors import AIError, MediaError
from clipfactory.media import ffmpeg
from clipfactory.media.ingest import twitch_channel_url
from clipfactory.models import AudioEvent, Candidate, Clip, Feedback, LiveMoment, Project, VisualEvent
from clipfactory.pipeline.common import Inputs, build_analyst, load_features, load_inputs, load_segments, purge_clip_files
from clipfactory.pipeline.context import JobContext
from clipfactory.render.captions import build_captions
from clipfactory.timeutil import clamp_ms, clock, s_to_ms

DEFAULT_HASHTAGS = {
    "gta6": ["#gta6", "#gtavi", "#vicecity", "#gaming"],
    "gta_rp": ["#gtarp", "#gta5", "#fivem", "#gaming"],
    "gta_action": ["#gta5", "#gtaonline", "#gaming", "#clips"],
    "general": ["#gaming", "#clips", "#stream"],
    "custom": ["#gaming", "#clips"],
}


def credit_social(social: dict, channel: str | None, origin_url: str | None) -> dict:
    """Post copy that credits the channel the footage comes from: a line under the caption and a hashtag.

    Deterministic on purpose — who made the video is a fact of the source, not something to ask a model.
    """
    if not channel:
        return social
    on_twitch = origin_url == twitch_channel_url(channel)  # recorded live from that channel's page
    credit = f"🎥 twitch.tv/{channel}" if on_twitch else f"🎥 {channel}"
    tag = "#" + "".join(ch for ch in channel.lower() if ch.isalnum() or ch == "_")
    hashtags = list(social.get("hashtags") or [])
    if len(tag) > 1 and tag not in (h.lower() for h in hashtags):
        hashtags = [*hashtags[:9], tag]
    return {**social, "caption": "\n".join(filter(None, [social.get("caption"), credit])), "hashtags": hashtags}


def _editorial_context(inputs: Inputs) -> EditorialContext:
    p = inputs.settings
    return EditorialContext(p.profile, p.instructions, p.min_len_s, p.max_len_s, p.preset)


def _signals(ctx: JobContext, inputs: Inputs, segments: list[Segment]) -> tuple[cand.Signals, list[int], list[tuple[int, int]]]:
    audio, visual = load_features(ctx.settings, inputs.source_id)
    signals = cand.build_signals(inputs.duration_ms, audio, visual, speech_stats(segments, inputs.duration_ms))
    cuts = [int(c) for c in visual.cuts_ms] if visual is not None else []
    silences = silence_intervals(audio, ctx.runtime.audio) if audio is not None else []
    return signals, cuts, silences


def _signal_notes(events: list[AudioEvent], motion: list[VisualEvent], start_ms: int, end_ms: int, limit: int = 24) -> str:
    inside = [e for e in events if start_ms <= e.peak_ms < end_ms and e.kind != "silence"]
    inside = sorted(sorted(inside, key=lambda e: e.score, reverse=True)[:limit], key=lambda e: e.peak_ms)
    lines = [f"{e.peak_ms / 1000:.1f}s {e.kind} (confidence {e.score:.2f})" for e in inside]
    lines += [f"{m.start_ms / 1000:.1f}s-{m.end_ms / 1000:.1f}s {m.kind}" for m in motion
              if m.kind in ("high_motion", "static") and m.start_ms < end_ms and m.end_ms > start_ms][:12]  # fmt: skip
    return "\n".join(lines) or "(nothing notable measured)"


def _transcript_chunks(segments: list[Segment], duration_ms: int, budget: AnalysisBudget,
                       events: list[AudioEvent], motion: list[VisualEvent]) -> list[tuple[int, int, str, str]]:  # fmt: skip
    """Bounded, overlapping transcript sections. Sections with almost no speech are not sent at all."""
    size, overlap = budget.transcript_chunk_s * 1000, budget.transcript_overlap_s * 1000
    chunks = []
    start = 0
    while start < duration_ms:
        end = min(duration_ms, start + size)
        inside = [s for s in segments if s.end_ms > start and s.start_ms < end]
        if sum(len(s.text.split()) for s in inside) >= 30:
            chunks.append((start, end, cand.transcript_excerpt(segments, start, end), _signal_notes(events, motion, start, end)))
        if end >= duration_ms:
            break
        start = end - overlap
    return chunks


def _frame_times(signals: cand.Signals, start_ms: int, end_ms: int, count: int) -> list[int]:
    """Adaptive sampling: sparse across the window for context, dense around the suspected action."""
    a, b = start_ms // 1000, max(start_ms // 1000 + 1, end_ms // 1000)
    peak_ms = (a + int(np.argmax(signals.interest[a:b]))) * 1000 + 500 if b <= signals.seconds else (start_ms + end_ms) // 2
    sparse = math.ceil(count * 0.4)
    dense = count - sparse
    times = [start_ms + int((end_ms - start_ms) * (i + 0.5) / sparse) for i in range(sparse)]
    lo, hi = max(start_ms, peak_ms - 3000), min(end_ms - 100, peak_ms + 3000)
    times += [lo + int((hi - lo) * (i + 0.5) / dense) for i in range(dense)] if dense and hi > lo else []
    out: list[int] = []
    for t in sorted(times):
        if not out or t - out[-1] >= 300:
            out.append(t)
    return out


def _frames(ctx: JobContext, inputs: Inputs, times: list[int], width: int) -> list[tuple[int, Path]]:
    folder = ctx.settings.path("cache", "frames", inputs.source_id)
    frames = []
    for t in times:
        path = folder / f"{t}_{width}.jpg"
        if not path.exists():
            try:
                ffmpeg.extract_frame(inputs.source_path, t, path, width=width)
            except MediaError:
                continue
        frames.append((t, path))
    return frames


def stage_analyze(ctx: JobContext) -> None:
    inputs = load_inputs(ctx)
    p = inputs.settings
    budget = BUDGETS[p.preset]
    min_ms, max_ms = p.min_len_s * 1000, p.max_len_s * 1000
    with session_scope() as s:
        segments, _ = load_segments(s, inputs.source_id)
        events = list(s.scalars(select(AudioEvent).where(AudioEvent.source_id == inputs.source_id)))
        motion = list(s.scalars(select(VisualEvent).where(VisualEvent.source_id == inputs.source_id, VisualEvent.origin == "heuristic",
                                                          VisualEvent.kind != "scene_cut")))  # fmt: skip
        s.expunge_all()
    signals, _cuts, _silences = _signals(ctx, inputs, segments)
    ectx = _editorial_context(inputs)
    analyst, gateway = build_analyst(ctx, announce=True)

    # 1 ── broad discovery: more candidates than clips requested
    pool_target = max(p.clip_count + 4, math.ceil(p.clip_count * budget.pool_factor))
    windows = cand.signal_candidates(signals, pool_target, min_ms, max_ms)
    ctx.log(f"{len(windows)} candidats issus des signaux audio/visuels")
    with session_scope() as s:
        moment = s.scalar(select(LiveMoment).where(LiveMoment.project_id == ctx.project_id))
        chat = (moment.chatters, moment.baseline) if moment else None
    if chat:
        # A source recorded around a chat spike: what chat reacted to is at its end, and must always compete.
        windows.append(cand.CandidateWindow(max(0, inputs.duration_ms - max_ms), inputs.duration_ms, ["chat_spike"], event_type="chat_spike"))
        ctx.log(f"moment signalé par le chat : {chat[0]} personnes en 10 s (habituel : {chat[1]:.0f})")
    ctx.progress(0.1, "analyse du transcript")

    if analyst is not None and not budget.transcript_moments:
        ctx.log("analyse IA du transcript et des images non demandée par ce préréglage")
    elif analyst is not None and segments:
        chunks = _transcript_chunks(segments, inputs.duration_ms, budget, events, motion)
        per_chunk = max(3, math.ceil(pool_target / max(1, len(chunks))) + 1)
        moments = analyst.find_moments(chunks, per_chunk, ectx) if chunks else []
        kept = 0
        for m in moments:
            start, end = s_to_ms(m.start_s), s_to_ms(m.end_s)
            # Deterministic validation: model timestamps must lie inside the source and be a real interval.
            if not (0 <= start < end <= inputs.duration_ms + 1000) or end - start < 3000:
                ctx.log(f"moment rejeté (horodatage invalide {m.start_s:.1f}s–{m.end_s:.1f}s)", "warning")
                continue
            end = min(end, inputs.duration_ms)
            if end - start > max_ms * 1.25:
                start = end - max_ms  # keep the payoff, trim the setup
            windows.append(cand.CandidateWindow(
                start, end, ["transcript_llm"], title=m.title[:300], event_type=m.event_type[:48],
                structure={"setup": m.setup, "payoff": m.payoff, "why": m.why_entertaining}, confidence=m.confidence,
            ))  # fmt: skip
            kept += 1
        ctx.log(f"{kept} moments proposés par l'analyse du transcript ({len(chunks)} sections)")
    ctx.should_stop()

    merged = cand.merge_candidates(windows)
    rows: list[Candidate] = []
    with session_scope() as s:
        project = s.get(Project, ctx.project_id)
        for clip in list(project.clips):
            purge_clip_files(ctx.settings, s, clip)
            s.delete(clip)
        s.execute(delete(Candidate).where(Candidate.project_id == ctx.project_id))
        for w in merged:
            features = cand.window_features(signals, w.start_ms, w.end_ms)
            rows.append(Candidate(
                project_id=ctx.project_id, start_ms=w.start_ms, end_ms=w.end_ms, origins=w.origins, title=w.title,
                event_type=w.event_type, structure=w.structure or None,
                evidence={"features": features, "moment_confidence": w.confidence},
                heuristic_score=cand.heuristic_score(features),
            ))  # fmt: skip
        s.add_all(rows)
        s.flush()
        s.expunge_all()
    ctx.log(f"{len(rows)} candidats après fusion des doublons")
    ctx.progress(0.35, f"{len(rows)} candidats")

    # 2 ── multimodal understanding, only for the most promising candidates
    analysed = 0
    top_k = min(len(rows), budget.visual_cap, math.ceil(p.clip_count * budget.visual_factor))
    if analyst is not None and top_k:
        shortlist = sorted(rows, key=lambda c: (c.heuristic_score or 0) + (0.15 if "transcript_llm" in c.origins else 0), reverse=True)[:top_k]
        use_video = budget.video_input and ctx.runtime.use_video_input and not analyst.is_test
        items: list[VisualInput] = []
        for i, c in enumerate(shortlist):
            ctx.should_stop()
            ctx.progress(0.35 + 0.25 * i / len(shortlist), "extraction d'images clés")
            frames = _frames(ctx, inputs, _frame_times(signals, c.start_ms, c.end_ms, budget.frames_per_candidate), budget.frame_width)
            video = None
            if use_video and c.end_ms - c.start_ms <= 90_000:
                video = ctx.settings.path("cache", "frames", inputs.source_id, f"{c.start_ms}_{c.end_ms}.mp4")
                if not video.exists():
                    try:
                        ffmpeg.make_analysis_clip(inputs.source_path, video, start_ms=c.start_ms, end_ms=c.end_ms)
                    except MediaError:
                        video = None
            if frames or video:
                items.append(VisualInput(c.id, c.start_ms, c.end_ms, frames, video,
                                         cand.transcript_excerpt(segments, c.start_ms, c.end_ms, relative_to=c.start_ms),
                                         _signal_notes(events, motion, c.start_ms, c.end_ms, 8)))  # fmt: skip
        ctx.progress(0.6, f"analyse visuelle de {len(items)} candidats")
        try:
            described = analyst.describe_visual(items, ectx)
        except AIError as exc:
            if exc.code in ("ai_auth", "ai_insufficient_credits", "ai_budget_exceeded"):
                raise
            ctx.warn(f"analyse visuelle indisponible ({exc.code}) : classement sans composante visuelle")
            described = {}
        with session_scope() as s:
            s.execute(delete(VisualEvent).where(VisualEvent.candidate_id.in_([c.id for c in rows])))
            for c in shortlist:
                analysis = described.get(c.id)
                if analysis is None:
                    continue
                row = s.get(Candidate, c.id)
                data = analysis.model_dump()
                row.evidence = {**(row.evidence or {}), "visual": data}
                row.multimodal_score = ranking.multimodal_from_visual(data)
                s.add(VisualEvent(source_id=inputs.source_id, candidate_id=c.id, origin="model", kind=analysis.event_type[:48],
                                  start_ms=c.start_ms, end_ms=c.end_ms, confidence=analysis.confidence, description=data))  # fmt: skip
                analysed += 1
        ctx.log(f"analyse multimodale : {analysed}/{len(shortlist)} candidats décrits")

    ctx.metrics(candidates=len(rows), multimodal=analysed, ai=analyst is not None,
                spent_usd=round(gateway.spent_usd, 4) if gateway else 0)  # fmt: skip
    ctx.progress(1.0, f"{len(rows)} candidats analysés")


def _heuristic_justification(features: dict, start_ms: int) -> str:
    return (
        f"Score heuristique, sans analyse IA : pic d'activité autour de {clock(start_ms)}, parole sur "
        f"{features.get('speech_coverage', 0) * 100:.0f} % du segment, {features.get('dead_time', 0) * 100:.0f} % de temps mort."
    )


def _mean_confidence(segments: list[Segment], start_ms: int, end_ms: int) -> float | None:
    values = [s.confidence for s in segments if s.confidence is not None and s.end_ms > start_ms and s.start_ms < end_ms]
    return sum(values) / len(values) if values else None


def rank_briefs(rows: list[Candidate], segments: list[Segment]) -> list[ClipBrief]:
    """What the editorial model is shown for each candidate.

    Built only from discovery-time data (bounds, transcript, measured features, visual analysis, the
    proposed moment). Nothing the ranking stage itself writes may appear here: these briefs are the
    cache key of the ranking calls, so re-ranking with new thresholds or weights costs nothing.
    """
    return [
        ClipBrief(
            candidate_id=c.id, duration_s=(c.end_ms - c.start_ms) / 1000,
            transcript=cand.transcript_excerpt(segments, c.start_ms, c.end_ms, relative_to=c.start_ms),
            features=(c.evidence or {}).get("features"), visual=(c.evidence or {}).get("visual"),
            moment={"title": c.title, **(c.structure or {})} if c.title else None,
        )  # fmt: skip
        for c in rows
    ]


def stage_rank(ctx: JobContext) -> None:
    inputs = load_inputs(ctx)
    p = inputs.settings
    with session_scope() as s:
        segments, _ = load_segments(s, inputs.source_id)
        rows = list(s.scalars(select(Candidate).where(Candidate.project_id == ctx.project_id).order_by(Candidate.start_ms, Candidate.id)))
        history = [
            (f.event_type or "unknown", f.decision)
            for f in s.scalars(select(Feedback).where(Feedback.profile == p.profile))
        ]
        s.expunge_all()
    if not rows:
        with session_scope() as s:
            s.get(Project, ctx.project_id).summary = {
                "explanation": "Aucun moment exploitable n'a été trouvé dans cette source.", "evaluated": 0, "selected": 0, "basis": "none",
            }  # fmt: skip
        ctx.warn("aucun candidat à classer")
        return

    analyst, gateway = build_analyst(ctx)
    scores = {}
    if analyst is not None and not BUDGETS[p.preset].editorial_ranking:
        ctx.log("notation IA non demandée par ce préréglage : classement heuristique")
    elif analyst is not None:
        briefs = rank_briefs(rows, segments)
        ctx.progress(0.2, f"notation éditoriale de {len(briefs)} candidats")
        scores = analyst.score(briefs, _editorial_context(inputs))
        ctx.log(f"notation éditoriale reçue pour {len(scores)}/{len(rows)} candidats")

    weights = ranking.rubric_weights(p.profile, ctx.runtime.profile_weights)
    adjustments = ranking.feedback_adjustments(history)
    scored = []
    for c in rows:
        evidence = c.evidence or {}
        features = evidence.get("features") or {}
        score = scores.get(c.id)
        visual = evidence.get("visual")
        confidences = [x for x in (score.confidence if score else None, visual.get("confidence") if visual else None) if x is not None]
        item = ranking.ScoreInput(
            candidate_id=c.id, start_ms=c.start_ms, end_ms=c.end_ms, features=features, heuristic=c.heuristic_score or 0.0,
            multimodal=c.multimodal_score,
            editorial=ranking.editorial_from_rubric(score.model_dump(), weights) if score else None,
            missing_context=score.missing_context if score else 0.0,
            confidence=min(confidences) if confidences else None,
            transcript_confidence=_mean_confidence(segments, c.start_ms, c.end_ms),
            event_type=(score.event_type if score else c.event_type)[:48],
            text=" ".join(sg.text for sg in segments if sg.end_ms > c.start_ms and sg.start_ms < c.end_ms),
        )  # fmt: skip
        scored.append((item, ranking.score_candidate(item, adjustments), score))

    selection = ranking.select_clips([(i, r) for i, r, _ in scored], count=p.clip_count, threshold=p.quality_threshold)
    is_test = bool(analyst and analyst.is_test)
    with session_scope() as s:
        for item, result, score in scored:
            row = s.get(Candidate, item.candidate_id)
            row.editorial_score = item.editorial
            row.final_score = result.score
            row.penalties = result.penalties
            row.event_type = item.event_type
            row.evidence = {**(row.evidence or {}), "components": result.components, "basis": "test" if is_test else result.basis,
                            "rubric": score.model_dump(exclude={"candidate_id", "justification", "main_event"}) if score else None,
                            **result.extra}  # fmt: skip
            if score:
                # Ranking output goes to its own fields. It must never overwrite what rank_briefs reads
                # (title, structure, features, visual), or the next run would miss the cache and pay again.
                row.justification = score.justification
                row.evidence = {**row.evidence, "main_event": score.main_event}
            else:
                row.justification = _heuristic_justification(item.features, item.start_ms)
            if item.candidate_id in selection.selected:
                row.status, row.reject_reason = "selected", None
            else:
                row.status, row.reject_reason = "rejected", selection.rejected.get(item.candidate_id)
        s.get(Project, ctx.project_id).summary = {
            "explanation": selection.explanation, "evaluated": len(scored), "selected": len(selection.selected),
            "requested": p.clip_count, "threshold": p.quality_threshold,
            "basis": "test" if is_test else ("ai" if scores else "heuristic"),
            "feedback_adjustments": adjustments,
        }  # fmt: skip
    ctx.log(selection.explanation)
    ctx.metrics(evaluated=len(scored), selected=len(selection.selected), spent_usd=round(gateway.spent_usd, 4) if gateway else 0)
    ctx.progress(1.0, f"{len(selection.selected)} clips retenus sur {len(scored)} candidats")


def stage_refine(ctx: JobContext) -> None:
    inputs = load_inputs(ctx)
    p = inputs.settings
    budget = BUDGETS[p.preset]
    with session_scope() as s:
        segments, transcript_is_test = load_segments(s, inputs.source_id)
        chosen = list(s.scalars(select(Candidate).where(Candidate.project_id == ctx.project_id, Candidate.status == "selected")
                                .order_by(Candidate.final_score.desc())))  # fmt: skip
        s.expunge_all()
    signals, cuts, silences = _signals(ctx, inputs, segments)
    min_ms, max_ms = p.min_len_s * 1000, p.max_len_s * 1000

    def refine(start: int, end: int) -> tuple[int, int, list[str]]:
        return refine_boundaries(start, end, segments=segments, silences=silences, cuts_ms=cuts, duration_ms=inputs.duration_ms,
                                 min_len_ms=min(min_ms, inputs.duration_ms), max_len_ms=max_ms)  # fmt: skip

    bounds: dict[str, tuple[int, int, list[str]]] = {c.id: refine(c.start_ms, c.end_ms) for c in chosen}
    analyst, gateway = build_analyst(ctx) if chosen else (None, None)
    ectx = _editorial_context(inputs)

    def brief(c: Candidate, with_context: bool) -> ClipBrief:
        start, end, _ = bounds[c.id]
        proposed = {"title": c.title, "main_event": (c.evidence or {}).get("main_event")}
        return ClipBrief(
            candidate_id=c.id, duration_s=(end - start) / 1000,
            transcript=cand.transcript_excerpt(segments, start, end, relative_to=start),
            # A moment found by signals has no title yet: an empty one would be read as a flaw of the clip.
            moment={key: value for key, value in proposed.items() if value},
            context_before=cand.transcript_excerpt(segments, start - 6000, start, relative_to=start) if with_context else "",
            context_after=cand.transcript_excerpt(segments, end, end + 6000, relative_to=start) if with_context else "",
        )  # fmt: skip

    rejected: dict[str, str] = {}
    if analyst is not None and budget.critique and chosen:
        ctx.progress(0.3, "contrôle qualité final")
        try:
            for cid, critique in analyst.critique([brief(c, True) for c in chosen], ectx).items():
                start, end, notes = bounds[cid]
                if critique.verdict == "reject":
                    issue = critique.issue or "rejeté au contrôle qualité final"
                    if p.quality_threshold > 0:
                        rejected[cid] = issue
                    else:
                        # No quality threshold: the person reviewing the clip is the gate, and sees the objection.
                        bounds[cid] = (start, end, [*notes, f"réserve du contrôle qualité : {issue}"])
                elif critique.verdict == "adjust":
                    # Bounded, then re-validated by the same deterministic rules as every other boundary.
                    new_start = clamp_ms(start + s_to_ms(max(-6, min(6, critique.start_offset_s))), 0, inputs.duration_ms)
                    new_end = clamp_ms(end + s_to_ms(max(-6, min(6, critique.end_offset_s))), 0, inputs.duration_ms)
                    if new_end - new_start >= 3000:
                        a, b, more = refine(new_start, new_end)
                        bounds[cid] = (a, b, [*notes, f"ajusté au contrôle qualité : {critique.issue}", *more])
        except AIError as exc:
            if exc.code in ("ai_auth", "ai_insufficient_credits", "ai_budget_exceeded"):
                raise
            ctx.warn(f"contrôle qualité final indisponible ({exc.code})")

    kept = [c for c in chosen if c.id not in rejected]
    social = {}
    if analyst is not None and kept:
        ctx.progress(0.6, "titres et légendes")
        try:
            social = analyst.social([brief(c, False) for c in kept], ectx)
        except AIError as exc:
            if exc.code in ("ai_auth", "ai_insufficient_credits", "ai_budget_exceeded"):
                raise
            ctx.warn(f"génération des légendes indisponible ({exc.code})")

    is_test = bool(analyst and analyst.is_test) or transcript_is_test
    with session_scope() as s:
        project = s.get(Project, ctx.project_id)
        for clip in list(project.clips):
            purge_clip_files(ctx.settings, s, clip)
            s.delete(clip)
        for cid, reason in rejected.items():
            row = s.get(Candidate, cid)
            row.status, row.reject_reason = "rejected", reason[:200]
        for rank, c in enumerate(kept, start=1):
            start, end, notes = bounds[c.id]
            captions, precision = build_captions(segments, start, end, p.subtitle_style)
            copy = social.get(c.id)
            evidence = c.evidence or {}
            main_event = evidence.get("main_event") or c.title
            first_words = " ".join(" ".join(sg.text for sg in segments if sg.end_ms > start and sg.start_ms < end).split()[:9])
            # No model title and nothing spoken (pure gameplay): name the clip by where it is in the source.
            fallback_title = c.title or main_event or first_words or f"Moment à {clock(start)}"
            s.add(Clip(
                project_id=ctx.project_id, candidate_id=c.id, rank=rank, start_ms=start, end_ms=end,
                original_start_ms=start, original_end_ms=end, score=c.final_score or 0,
                score_basis=evidence.get("basis", "heuristic"), title=(copy.title if copy else fallback_title)[:300],
                main_event=(main_event or "")[:300], justification=c.justification, layout=p.layout,
                subtitle_style=p.subtitle_style, subtitle_options=p.subtitle_options.model_dump(),
                captions=[cp.model_dump() for cp in captions], captions_precision=precision,
                social=credit_social(
                    {"title": copy.title, "caption": copy.caption, "hashtags": copy.hashtags, "origin": "test" if is_test else "ai"}
                    if copy else {"title": fallback_title, "caption": first_words, "hashtags": DEFAULT_HASHTAGS[p.profile],
                                  "origin": "default"},
                    inputs.channel, inputs.origin_url,
                ),
                is_test=is_test,
            ))  # fmt: skip
            row = s.get(Candidate, c.id)
            row.evidence = {**(row.evidence or {}), "refinement": notes, "refined": [start, end]}
        if rejected:
            summary = dict(project.summary or {})
            summary["selected"] = len(kept)
            summary["explanation"] = f"{summary.get('explanation', '')} {len(rejected)} clip(s) écarté(s) au contrôle qualité final.".strip()
            project.summary = summary
    ctx.metrics(clips=len(kept), rejected_by_critique=len(rejected), spent_usd=round(gateway.spent_usd, 4) if gateway else 0)
    ctx.log(f"{len(kept)} clips prêts pour le rendu")
    ctx.progress(1.0, f"{len(kept)} clips")
