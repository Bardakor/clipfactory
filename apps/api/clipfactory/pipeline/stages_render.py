"""Render and validation stages, shared by full pipeline jobs and single-clip re-renders."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from sqlalchemy import func, select

from clipfactory.db import session_scope
from clipfactory.domain import Caption, Rect, ResolvedLayout
from clipfactory.errors import ClipFactoryError, ExportInvalid, JobCancelled, RenderError, WorkerShutdown
from clipfactory.models import Clip, InferenceUsage, Job, JobStage, Project, Render
from clipfactory.pipeline.common import load_features, load_inputs, load_segments, replace_assets
from clipfactory.pipeline.context import JobContext
from clipfactory.render.renderer import RenderedFile, RenderSpec, publish_export, render_clip, slugify, validate_render
from clipfactory.timeutil import clock


def _job_payload(ctx: JobContext) -> dict:
    with session_scope() as s:
        return dict(s.get(Job, ctx.job_id).payload or {})


def _target_clip_ids(ctx: JobContext) -> tuple[list[str], str]:
    payload = _job_payload(ctx)
    mode = payload.get("mode", "final")
    if payload.get("clip_id"):
        return [payload["clip_id"]], mode
    with session_scope() as s:
        return list(s.scalars(select(Clip.id).where(Clip.project_id == ctx.project_id).order_by(Clip.rank))), mode


def build_spec(ctx: JobContext, clip_id: str, mode: str) -> RenderSpec:
    inputs = load_inputs(ctx)
    _, visual = load_features(ctx.settings, inputs.source_id)
    with session_scope() as s:
        clip = s.get(Clip, clip_id)
        manual = (clip.layout_params or {}).get("manual")
        return RenderSpec(
            clip_id=clip.id, source_path=inputs.source_path, source_fingerprint=inputs.fingerprint or "",
            src_has_audio=inputs.has_audio, start_ms=clip.start_ms, end_ms=clip.end_ms, width=inputs.settings.width,
            height=inputs.settings.height, fps=inputs.settings.fps, layout=clip.layout,
            manual_rect=Rect.model_validate(manual) if manual else None, facecam=inputs.facecam,
            cuts_ms=[int(c) for c in visual.cuts_ms] if visual is not None else [],
            subtitle_style=clip.subtitle_style, subtitle_options=clip.subtitle_options or {},
            captions=[Caption.model_validate(c) for c in clip.captions or []],
            burn_subtitles=inputs.settings.burn_subtitles, mode=mode, remotion_concurrency=ctx.runtime.remotion_concurrency,
        )  # fmt: skip


def stage_render(ctx: JobContext) -> None:
    clip_ids, mode = _target_clip_ids(ctx)
    if not clip_ids:
        ctx.log("aucun clip à rendre")
        return
    with session_scope() as s:
        # A resumed job must not render again what it already rendered and has not yet validated.
        done = {r.clip_id for r in s.scalars(select(Render).where(Render.job_id == ctx.job_id, Render.status == "rendered"))
                if Path((r.settings or {}).get("path", "/nonexistent")).exists()}  # fmt: skip
        if mode == "final":  # a preview never changes the state of the clip's final export
            for cid in clip_ids:
                if cid not in done:
                    s.get(Clip, cid).render_status = "rendering"
    todo = [cid for cid in clip_ids if cid not in done]
    fractions = {cid: 1.0 for cid in done} | {cid: 0.0 for cid in todo}

    def report(cid: str, fraction: float, detail: str) -> None:
        fractions[cid] = fraction
        finished = sum(1 for f in fractions.values() if f >= 1.0)
        ctx.progress(sum(fractions.values()) / len(fractions), f"clip {min(finished + 1, len(fractions))}/{len(fractions)} · {detail}")

    def one(cid: str) -> tuple[str, RenderedFile | ClipFactoryError]:
        try:
            spec = build_spec(ctx, cid, mode)
            rendered = render_clip(ctx.settings, spec, on_progress=lambda f, d: report(cid, f * 0.999, d), should_stop=ctx.should_stop)
            report(cid, 1.0, "rendu terminé")
            return cid, rendered
        except (JobCancelled, WorkerShutdown):
            raise
        except ClipFactoryError as exc:
            report(cid, 1.0, "échec")
            return cid, exc

    failures = 0
    notes: list[tuple[str, str, dict]] = []  # logged after each transaction closes: SQLite has one writer
    # Bounded parallelism: each Remotion render already uses several browser tabs and a few GB of memory.
    with ThreadPoolExecutor(max_workers=ctx.runtime.render_concurrency, thread_name_prefix="render") as pool:
        futures = [pool.submit(one, cid) for cid in todo]
        for future in as_completed(futures):
            cid, outcome = future.result()
            with session_scope() as s:
                clip = s.get(Clip, cid)
                if isinstance(outcome, ClipFactoryError):
                    failures += 1
                    if mode == "final":
                        clip.render_status = "failed"
                        clip.validation = {"ok": False, "error": outcome.to_dict()}
                    s.add(Render(clip_id=cid, job_id=ctx.job_id, mode=mode, status="failed", error=outcome.to_dict()))
                    notes.append((f"rendu échoué pour le clip {clip.rank} : {outcome.message}", "error", {"details": str(outcome.details)[:1500]}))
                else:
                    if mode == "final":
                        clip.render_status = "validating"
                    clip.resolved_layout = outcome.layout.model_dump()
                    s.add(Render(
                        clip_id=cid, job_id=ctx.job_id, mode=mode, status="rendered", duration_ms=int(outcome.render_seconds * 1000),
                        settings={"path": str(outcome.path), "cut_path": str(outcome.cut_path), "expected_ms": outcome.expected_ms,
                                  "width": outcome.width, "height": outcome.height, "fps": outcome.fps,
                                  "layout": outcome.layout.model_dump()},
                    ))  # fmt: skip
                    notes.append((f"clip {clip.rank} rendu en {outcome.render_seconds:.1f}s (mise en page : {outcome.layout.used} — {outcome.layout.reason})", "info", {}))
            while notes:
                message, level, data = notes.pop(0)
                ctx.log(message, level, **data)
    ctx.metrics(rendered=len(todo) - failures, failed=failures, mode=mode)
    if todo and failures == len(todo):
        raise RenderError("every clip failed to render — see the job log for each error")


def _clip_metadata(ctx: JobContext, clip: Clip, project: Project, segments: list, validation: dict, layout: dict) -> dict:
    src = project.source
    return {
        "clip_id": clip.id,
        "is_test": clip.is_test,
        "title": clip.title,
        "main_event": clip.main_event,
        "editorial_score": {
            "value": clip.score, "scale": "0-100", "basis": clip.score_basis,
            "note": "Editorial estimate of short-form potential. Not a probability of virality.",
            "justification": clip.justification,
        },  # fmt: skip
        "social": clip.social,
        "source": {
            "title": src.title, "channel": src.channel, "url": src.origin_url, "start_ms": clip.start_ms, "end_ms": clip.end_ms,
            "start": clock(clip.start_ms), "end": clock(clip.end_ms),
        },  # fmt: skip
        "layout": {"requested": clip.layout, "used": layout.get("used"), "reason": layout.get("reason")},
        "subtitles": {"style": clip.subtitle_style, "precision": clip.captions_precision, "edited": clip.captions_edited,
                      "captions": clip.captions},  # fmt: skip
        "transcript": [
            {"start_ms": sg.start_ms - clip.start_ms, "end_ms": sg.end_ms - clip.start_ms, "source_start_ms": sg.start_ms,
             "text": sg.text, "language": sg.language, "provenance": sg.provenance}  # fmt: skip
            for sg in segments if sg.end_ms > clip.start_ms and sg.start_ms < clip.end_ms
        ],
        "validation": validation,
    }


def stage_validate(ctx: JobContext) -> None:
    clip_ids, mode = _target_clip_ids(ctx)
    with session_scope() as s:
        pending = list(s.scalars(select(Render).where(Render.job_id == ctx.job_id, Render.status == "rendered")))
        s.expunge_all()
    if not pending:
        ctx.log("aucun rendu à valider")
    valid = 0
    for i, render in enumerate(pending):
        ctx.should_stop()
        ctx.progress(i / len(pending), f"validation du clip {i + 1}/{len(pending)}")
        cfg = render.settings or {}
        spec = build_spec(ctx, render.clip_id, mode)
        rendered = RenderedFile(Path(cfg["path"]), Path(cfg["cut_path"]), ResolvedLayout.model_validate(cfg["layout"]),
                                cfg["expected_ms"], cfg["width"], cfg["height"], cfg["fps"], 0.0)  # fmt: skip
        validation = validate_render(spec, rendered)
        note: tuple[str, str] = ("", "info")
        with session_scope() as s:
            row = s.get(Render, render.id)
            clip = s.get(Clip, render.clip_id)
            project = s.get(Project, ctx.project_id)
            row.validation = validation.model_dump()
            if not validation.ok:
                # Never reported as completed: the file stays in the work directory for inspection.
                row.status = "invalid"
                if mode == "final":
                    clip.render_status = "failed"
                    clip.validation = validation.model_dump()
                note = (f"clip {clip.rank} invalide : {', '.join(validation.failures)} — {validation.details}", "error")
            else:
                segments, _ = load_segments(s, project.source_id)
                stem = f"{clip.rank:02d}_{slugify(clip.title)}"
                files = publish_export(
                    ctx.settings, spec, rendered, validation, project_id=project.id, stem=stem,
                    metadata=_clip_metadata(ctx, clip, project, segments, validation.model_dump(), cfg["layout"]),
                    extra_copy_dir=ctx.runtime.export_dir if mode == "final" else None,
                )  # fmt: skip
                ids = replace_assets(s, ctx.settings, clip.id, files)
                row.status = "validated"
                row.output_asset_id = ids.get("export") or ids.get("preview")
                if mode == "final":
                    clip.render_status = "rendered"
                    clip.validation = validation.model_dump()
                valid += 1
                note = (f"clip {clip.rank} validé : {validation.details.get('duration_ms')} ms, {validation.details.get('frames')} images, "
                        f"décalage audio {validation.details.get('audio_offset_ms')} ms", "info")  # fmt: skip
        ctx.log(*note)
    ctx.metrics(validated=valid, invalid=len(pending) - valid)
    if _job_payload(ctx).get("clip_id") is None:
        write_performance_report(ctx)
    if pending and valid == 0:
        raise ExportInvalid("no rendered clip passed validation — see the job log")
    ctx.progress(1.0, f"{valid} export(s) validé(s)")


def write_performance_report(ctx: JobContext) -> dict:
    """Measured numbers for one pipeline run: per-stage wall time, inference usage, render throughput."""
    inputs = load_inputs(ctx)
    with session_scope() as s:
        stages = list(s.scalars(select(JobStage).where(JobStage.job_id == ctx.job_id).order_by(JobStage.position)))
        usage = s.execute(
            select(InferenceUsage.task, func.count(), func.sum(InferenceUsage.prompt_tokens), func.sum(InferenceUsage.completion_tokens),
                   func.sum(InferenceUsage.cost_usd_actual), func.sum(InferenceUsage.cost_usd_estimated))
            .where(InferenceUsage.job_id == ctx.job_id, InferenceUsage.status == "ok").group_by(InferenceUsage.task)
        ).all()  # fmt: skip
        renders = list(s.scalars(select(Render).where(Render.job_id == ctx.job_id, Render.status == "validated")))
        clip_ms = sum((s.get(Clip, r.clip_id).end_ms - s.get(Clip, r.clip_id).start_ms) for r in renders)
        render_ms = sum(r.duration_ms or 0 for r in renders)
        # The validate stage is still running while this is written, so it is absent from the total.
        processing_ms = sum(st.duration_ms or 0 for st in stages)
        report = {
            "job_id": ctx.job_id,
            "source_duration_ms": inputs.duration_ms,
            "processing_ms_excluding_validation": processing_ms,
            "realtime_factor": round(inputs.duration_ms / processing_ms, 2) if processing_ms else None,
            "stages": [{"name": st.name, "status": st.status, "duration_ms": st.duration_ms, "cache_hit": st.cache_hit,
                        "metrics": st.metrics} for st in stages],
            "inference": [{"task": t, "requests": n, "prompt_tokens": int(pt or 0), "completion_tokens": int(ct or 0),
                           "cost_usd_actual": round(ca, 6) if ca is not None else None,
                           "cost_usd_estimated": round(ce, 6) if ce is not None else None} for t, n, pt, ct, ca, ce in usage],
            "render": {"clips": len(renders), "clip_seconds": round(clip_ms / 1000, 1), "render_seconds": round(render_ms / 1000, 1),
                       "throughput_x": round(clip_ms / render_ms, 2) if render_ms else None},
        }  # fmt: skip
    path = ctx.settings.path("exports", inputs.project_id, "performance.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=1))
    return report
