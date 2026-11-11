"""Source-level stages: import, audio extraction, transcription, signal detection.

Their outputs depend only on the source (and detector / transcriber settings),
so any later project on the same video reuses them from cache.
"""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import delete

from clipfactory.analysis import audio as audio_analysis
from clipfactory.analysis import visual as visual_analysis
from clipfactory.db import session_scope
from clipfactory.errors import IngestError, MediaError
from clipfactory.media import ffmpeg, ingest
from clipfactory.models import AudioEvent, InferenceUsage, Project, Source, VisualEvent
from clipfactory.pipeline.common import audio_path, events_dir, load_inputs
from clipfactory.pipeline.context import JobContext
from clipfactory.render.layout import detect_facecam
from clipfactory.timeutil import clock
from clipfactory.transcription import service as transcription
from clipfactory.transcription.providers import build_transcriber


def stage_import(ctx: JobContext) -> None:
    inputs = load_inputs(ctx)
    if inputs.source_status != "ready":
        if inputs.source_kind != "youtube" or not inputs.origin_url:
            raise IngestError("the source file is missing; upload it again")
        video_id = ingest.parse_youtube_url(inputs.origin_url)
        ctx.log(f"téléchargement autorisé de {inputs.origin_url}")
        path = ingest.download_youtube(
            video_id, ctx.settings.path("sources"), inputs.source_id,
            max_bytes=int(ctx.settings.clipfactory_max_upload_gb * 1024**3),
            on_progress=lambda f: ctx.progress(0.95 * f, "téléchargement"), should_stop=ctx.should_stop,
        )  # fmt: skip
        duplicate = False
        with session_scope() as s:
            source = s.get(Source, inputs.source_id)
            try:
                final = ingest.finalize_source(s, ctx.settings, source, path)
            except MediaError as exc:
                path.unlink(missing_ok=True)
                failure = exc
            else:
                failure = None
                if final.id != source.id:  # same video already imported: reuse it and its caches
                    s.get(Project, ctx.project_id).source_id = final.id
                    duplicate = True
        if failure is not None:
            with session_scope() as s:
                s.get(Source, inputs.source_id).status = "failed"
            raise failure
        if duplicate:
            ctx.cache_hit("source déjà importée (même empreinte)")
        inputs = load_inputs(ctx)
    if inputs.source_path is None or not inputs.source_path.exists():
        raise IngestError("the source file is no longer on disk")
    ctx.metrics(duration_ms=inputs.duration_ms, width=inputs.width, height=inputs.height, has_audio=inputs.has_audio)
    ctx.log(f"source prête : {clock(inputs.duration_ms)}, {inputs.width}×{inputs.height}")
    ctx.progress(1.0, f"{clock(inputs.duration_ms)} · {inputs.width}×{inputs.height}")


def stage_extract_audio(ctx: JobContext) -> None:
    inputs = load_inputs(ctx)
    if not inputs.has_audio:
        ctx.warn("la source n'a pas de piste audio : transcription et analyse audio ignorées")
        return
    wav = audio_path(ctx.settings, inputs.source_id)
    if wav.exists() and wav.stat().st_size > 1024:
        ctx.cache_hit("piste audio d'analyse")
        return
    ffmpeg.extract_audio(inputs.source_path, wav, total_ms=inputs.duration_ms,
                         on_progress=lambda f: ctx.progress(f, "extraction audio 16 kHz"), should_stop=ctx.should_stop)  # fmt: skip
    ctx.metrics(bytes=wav.stat().st_size)


def stage_transcribe(ctx: JobContext) -> None:
    inputs = load_inputs(ctx)
    wav = audio_path(ctx.settings, inputs.source_id)
    if not wav.exists():
        ctx.log("pas d'audio : aucune transcription")
        return
    transcriber = build_transcriber(ctx.settings, ctx.runtime)
    with session_scope() as s:
        source = s.get(Source, inputs.source_id)
        key = transcription.transcript_cache_key(source, transcriber, inputs.settings.language, ctx.runtime)
        existing = transcription.find_transcript(s, inputs.source_id, key)
        if existing is not None:
            ctx.cache_hit(f"transcription ({existing.provider}, {len(existing.segments)} segments)")
            ctx.metrics(segments=len(existing.segments), precision=existing.precision, provider=existing.provider)
            return
        s.expunge(source)
    if transcriber.is_test:
        ctx.log("ADAPTATEUR DE TEST : transcription simulée, aucune reconnaissance vocale réelle")
    ctx.log(f"transcription avec {transcriber.name} ({transcriber.model})")
    result = transcription.run_transcription(
        ctx.settings, source, wav, transcriber, ctx.runtime, language=inputs.settings.language, cache_key=key,
        on_progress=ctx.progress, should_stop=ctx.should_stop,
    )  # fmt: skip
    with session_scope() as s:
        transcription.store_transcript(s, ctx.settings, s.get(Source, inputs.source_id), result, key)
        if result.cost_usd:
            s.add(InferenceUsage(project_id=ctx.project_id, job_id=ctx.job_id, task="transcription", model=result.model,
                                 status="ok", cost_usd_actual=result.cost_usd))  # fmt: skip
    if result.precision == "segment" and result.segments:
        ctx.warn("horodatage au mot indisponible avec ce fournisseur : sous-titres synchronisés par phrase, sans surlignage mot à mot")
    if not result.segments:
        ctx.warn("aucune parole détectée dans la source")
    ctx.metrics(segments=len(result.segments), precision=result.precision, provider=result.provider, language=result.language)


def stage_detect_events(ctx: JobContext) -> None:
    inputs = load_inputs(ctx)
    folder = events_dir(ctx.settings, inputs.source_id)
    counts: dict[str, int] = {}

    # ── audio ──
    wav = audio_path(ctx.settings, inputs.source_id)
    if wav.exists():
        cfg = ctx.runtime.audio
        key = hashlib.sha256(json.dumps([inputs.fingerprint, cfg.model_dump()]).encode()).hexdigest()
        npz = folder / f"audio_{key[:16]}.npz"
        if npz.exists():
            ctx.cache_hit("signaux audio")
            npz.touch()  # most recently used detector settings win when stages reload features
            features = audio_analysis.AudioFeatures.load(npz)
        else:
            features = audio_analysis.extract_features(wav, cfg, ctx.should_stop, lambda f: ctx.progress(0.3 * f, "analyse audio"))
            features.save(npz)
        with session_scope() as s:
            stored = s.query(AudioEvent).filter_by(source_id=inputs.source_id, cache_key=key).count()
        if not stored:  # the event rows always match the detector settings in use
            events = audio_analysis.detect_events(features, cfg)
            with session_scope() as s:
                s.execute(delete(AudioEvent).where(AudioEvent.source_id == inputs.source_id))
                s.add_all(AudioEvent(source_id=inputs.source_id, cache_key=key, kind=e.kind, start_ms=e.start_ms,
                                     end_ms=e.end_ms, peak_ms=e.peak_ms, score=round(e.score, 3), meta=e.meta) for e in events)  # fmt: skip
            for e in events:
                counts[e.kind] = counts.get(e.kind, 0) + 1

    # ── visual ──
    visual_npz = folder / "visual.npz"
    if visual_npz.exists():
        ctx.cache_hit("signaux visuels")
    else:
        features_v = visual_analysis.scan(inputs.source_path, inputs.duration_ms, ctx.should_stop,
                                          lambda f: ctx.progress(0.3 + 0.6 * f, "balayage visuel"))  # fmt: skip
        features_v.save(visual_npz)
        spans = visual_analysis.motion_spans(features_v)
        with session_scope() as s:
            s.execute(delete(VisualEvent).where(VisualEvent.source_id == inputs.source_id, VisualEvent.origin == "heuristic"))
            s.add_all(VisualEvent(source_id=inputs.source_id, origin="heuristic", kind=kind, start_ms=a, end_ms=b,
                                  confidence=round(conf, 3)) for kind, a, b, conf in spans)  # fmt: skip
            s.add_all(VisualEvent(source_id=inputs.source_id, origin="heuristic", kind="scene_cut", start_ms=int(c),
                                  end_ms=int(c), confidence=1.0) for c in features_v.cuts_ms[:5000])  # fmt: skip
        counts["scene_cut"] = len(features_v.cuts_ms)
        for kind, *_ in spans:
            counts[kind] = counts.get(kind, 0) + 1

    # ── facecam (once per source) ──
    if inputs.facecam is None:
        ctx.progress(0.92, "recherche d'une facecam")
        facecam = detect_facecam(inputs.source_path, inputs.duration_ms, ctx.settings.path("work", f"facecam_{inputs.source_id}"))
        with session_scope() as s:
            s.get(Source, inputs.source_id).facecam = facecam
        ctx.log("facecam détectée" if facecam.get("found") else "aucune facecam fiable détectée")
    if counts:
        ctx.metrics(events=counts)
        ctx.log("événements détectés : " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    ctx.progress(1.0, "signaux prêts")
