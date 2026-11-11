"""Job state machine: stage-by-stage execution with checkpointing, retries and cancellation.

A job only reaches ``succeeded`` when every stage did. An interrupted job goes
back to ``queued`` and resumes at its first unfinished stage; it is never
reported as done because the process happened to stop.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

import psutil
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from clipfactory.config import Settings
from clipfactory.db import session_scope
from clipfactory.domain import PIPELINE_STAGES, RENDER_STAGES
from clipfactory.errors import ClipFactoryError, JobCancelled, StageTimeout, WorkerShutdown
from clipfactory.logs import get_logger, redact
from clipfactory.models import Clip, Job, JobStage
from clipfactory.pipeline import stages_editorial, stages_render, stages_source
from clipfactory.pipeline.context import JobContext
from clipfactory.settings_store import load_runtime
from clipfactory.timeutil import now

log = get_logger("runner")

STAGES: dict[str, Callable[[JobContext], None]] = {
    "import": stages_source.stage_import,
    "extract_audio": stages_source.stage_extract_audio,
    "transcribe": stages_source.stage_transcribe,
    "detect_events": stages_source.stage_detect_events,
    "analyze": stages_editorial.stage_analyze,
    "rank": stages_editorial.stage_rank,
    "refine": stages_editorial.stage_refine,
    "render": stages_render.stage_render,
    "validate": stages_render.stage_validate,
}

# Automatic in-place retries for failures that are usually transient. Anything else
# fails the job and waits for the user's "retry".
AUTO_RETRIES = 1

TERMINAL = {"succeeded", "failed", "cancelled"}


def stage_names(kind: str) -> tuple[str, ...]:
    return PIPELINE_STAGES if kind == "pipeline" else RENDER_STAGES


def create_job(s: Session, *, project_id: str, kind: str, payload: dict | None = None, priority: int = 0,
               is_test: bool = False, clip_id: str | None = None) -> Job:  # fmt: skip
    job = Job(project_id=project_id, kind=kind, payload=payload, priority=priority, is_test=is_test, clip_id=clip_id)
    s.add(job)
    s.flush()
    s.add_all(JobStage(job_id=job.id, name=name, position=i) for i, name in enumerate(stage_names(kind)))
    return job


def claim_next_job(worker_id: str) -> str | None:
    """Atomically take the highest-priority queued job. Safe with several workers on one database."""
    with session_scope() as s:
        candidate = s.scalar(select(Job.id).where(Job.status == "queued").order_by(Job.priority.desc(), Job.created_at).limit(1))
        if candidate is None:
            return None
        claimed = s.execute(
            update(Job).where(Job.id == candidate, Job.status == "queued")
            .values(status="running", worker_id=worker_id, heartbeat_at=now(), started_at=now(), attempt=Job.attempt + 1,
                    error=None, finished_at=None)
        )  # fmt: skip
        return candidate if claimed.rowcount == 1 else None


def recover_interrupted_jobs(stale_after_s: float = 30.0) -> int:
    """Re-queue jobs left ``running`` by a worker that died. Completed stages are kept."""
    count = 0
    with session_scope() as s:
        for job in s.scalars(select(Job).where(Job.status == "running")):
            if job.heartbeat_at is None or (now() - job.heartbeat_at).total_seconds() > stale_after_s:
                _requeue(s, job)
                count += 1
    return count


def _requeue(s: Session, job: Job) -> None:
    job.status, job.worker_id = "queued", None
    for stage in job.stages:
        if stage.status == "running":
            stage.status, stage.progress, stage.detail = "pending", None, "interrompu — reprise"


def reset_for_retry(s: Session, job: Job, from_stage: str | None = None) -> None:
    """Queue a finished job again. Succeeded stages before ``from_stage`` keep their results."""
    reset = False
    for stage in job.stages:
        if stage.name == from_stage or (from_stage is None and stage.status != "succeeded"):
            reset = True
        if reset:
            stage.status, stage.progress, stage.detail, stage.error = "pending", None, None, None
            stage.started_at = stage.finished_at = stage.duration_ms = None
            stage.cache_hit, stage.metrics = False, None
    job.status, job.cancel_requested, job.error, job.finished_at = "queued", False, None, None
    job.warnings = None


def _finish_stage(job_id: str, name: str, status: str, started: float, error: dict | None = None, extra_metrics: dict | None = None) -> None:
    with session_scope() as s:
        stage = s.query(JobStage).filter_by(job_id=job_id, name=name).one()
        stage.status = status
        stage.finished_at = now()
        stage.duration_ms = int((time.monotonic() - started) * 1000)
        stage.error = error
        if status == "succeeded":
            stage.progress = 1.0
        if extra_metrics:
            stage.metrics = {**(stage.metrics or {}), **extra_metrics}


def _heartbeat(job_id: str, stop: threading.Event) -> None:
    while not stop.wait(5.0):
        try:
            with session_scope() as s:
                s.execute(update(Job).where(Job.id == job_id).values(heartbeat_at=now()))
        except Exception:  # noqa: BLE001 - a missed heartbeat must not kill the job
            log.exception("heartbeat failed")


def _run_with_timeout(fn: Callable[[JobContext], None], ctx: JobContext, timeout_s: float) -> None:
    """Stages check ``ctx.should_stop`` often, so a deadline is enforced the same way as cancellation."""
    deadline = time.monotonic() + timeout_s
    original = ctx.should_stop

    def guarded() -> None:
        if time.monotonic() > deadline:
            raise StageTimeout(f"stage '{ctx.stage}' exceeded its {timeout_s:.0f}s limit")
        original()

    ctx.should_stop = guarded  # type: ignore[method-assign]
    try:
        fn(ctx)
    finally:
        ctx.should_stop = original  # type: ignore[method-assign]


def run_job(settings: Settings, job_id: str, shutdown: threading.Event) -> str:
    """Execute a claimed job to a terminal state (or back to the queue on shutdown). Returns the final status."""
    with session_scope() as s:
        job = s.get(Job, job_id)
        runtime = load_runtime(s)
        project_id, kind = job.project_id, job.kind
        todo = [st.name for st in job.stages if st.status != "succeeded"]
    ctx = JobContext(settings, runtime, job_id, project_id, shutdown)
    stop_heartbeat = threading.Event()
    threading.Thread(target=_heartbeat, args=(job_id, stop_heartbeat), daemon=True).start()
    process = psutil.Process()
    final, error = "succeeded", None
    started = time.monotonic()
    try:
        for name in todo:
            ctx.begin_stage(name)
            ctx.should_stop()
            attempt = 0
            while True:
                started = time.monotonic()
                cpu_before = process.cpu_times()
                with session_scope() as s:
                    stage = s.query(JobStage).filter_by(job_id=job_id, name=name).one()
                    stage.status, stage.started_at, stage.error, stage.progress = "running", now(), None, None
                try:
                    _run_with_timeout(STAGES[name], ctx, runtime.stage_timeout_s)
                except ClipFactoryError as exc:
                    if exc.retryable and attempt < AUTO_RETRIES and not isinstance(exc, JobCancelled | WorkerShutdown):
                        attempt += 1
                        ctx.log(f"échec transitoire ({exc.code}) — nouvelle tentative {attempt}/{AUTO_RETRIES}", "warning")
                        continue
                    raise
                cpu = process.cpu_times()
                _finish_stage(job_id, name, "succeeded", started, extra_metrics={
                    "cpu_s": round((cpu.user - cpu_before.user) + (cpu.system - cpu_before.system), 1),
                    "rss_mb": round(process.memory_info().rss / 1e6),
                })  # fmt: skip
                break
    except WorkerShutdown:
        with session_scope() as s:
            _requeue(s, s.get(Job, job_id))
        ctx.stage = ""
        ctx.log("worker arrêté : le job reprendra à cette étape au prochain démarrage", "warning")
        stop_heartbeat.set()
        return "queued"
    except JobCancelled:
        final = "cancelled"
        _finish_stage(job_id, ctx.stage, "cancelled", started)
        ctx.log("job annulé par l'utilisateur", "warning")
    except ClipFactoryError as exc:
        final, error = "failed", exc.to_dict() | {"stage": ctx.stage}
        _finish_stage(job_id, ctx.stage, "failed", started, error=exc.to_dict())
        ctx.log(f"échec de l'étape {ctx.stage} : {exc.message}", "error", **{k: redact(str(v))[:800] for k, v in exc.details.items()})
    except Exception as exc:  # noqa: BLE001 - last line of defence: the job must end in a recorded state
        log.exception("unexpected failure in stage %s", ctx.stage)
        payload = {"code": "internal_error", "message": redact(f"{type(exc).__name__}: {exc}")[:500], "retryable": True, "details": {}}
        final, error = "failed", payload | {"stage": ctx.stage}
        _finish_stage(job_id, ctx.stage, "failed", started, error=payload)
        ctx.log(f"erreur inattendue à l'étape {ctx.stage} : {payload['message']}", "error")
    finally:
        stop_heartbeat.set()

    with session_scope() as s:
        job = s.get(Job, job_id)
        job.status, job.error, job.finished_at, job.worker_id = final, error, now(), None
        if final != "succeeded" and kind == "render" and job.clip_id:
            clip = s.get(Clip, job.clip_id)
            if clip is not None and clip.render_status in ("rendering", "validating", "queued"):
                clip.render_status = "failed"
    return final
