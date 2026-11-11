"""Job state machine: claiming, checkpointing, retry, cancellation, shutdown, crash recovery."""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest

from clipfactory.db import session_scope
from clipfactory.errors import MediaError, SubprocessFailed
from clipfactory.models import Job, JobLog, Project, Source
from clipfactory.pipeline import runner
from clipfactory.timeutil import now


@pytest.fixture
def job_id(settings) -> str:
    with session_scope() as s:
        source = Source(kind="upload", status="ready", title="t", duration_ms=1000)
        s.add(source)
        s.flush()
        project = Project(source_id=source.id, name="p", settings={})
        s.add(project)
        s.flush()
        return runner.create_job(s, project_id=project.id, kind="pipeline").id


def stages(job_id: str) -> dict[str, str]:
    with session_scope() as s:
        return {st.name: st.status for st in s.get(Job, job_id).stages}


def status(job_id: str) -> str:
    with session_scope() as s:
        return s.get(Job, job_id).status


@pytest.fixture
def calls(monkeypatch) -> list[str]:
    """Replace every real stage with a recorder."""
    seen: list[str] = []
    monkeypatch.setattr(runner, "STAGES", {name: (lambda ctx, n=name: seen.append(n)) for name in runner.PIPELINE_STAGES})
    return seen


def test_job_runs_every_stage_in_order(settings, job_id, calls):
    assert runner.claim_next_job("w1") == job_id
    assert runner.claim_next_job("w2") is None  # a claimed job cannot be claimed twice
    assert runner.run_job(settings, job_id, threading.Event()) == "succeeded"
    assert calls == list(runner.PIPELINE_STAGES)
    assert set(stages(job_id).values()) == {"succeeded"}


def test_failure_is_recorded_and_retry_resumes_at_the_failed_stage(settings, job_id, calls, monkeypatch):
    def boom(ctx):
        calls.append("analyze!")
        raise MediaError("corrupt frame", stderr="details")

    monkeypatch.setitem(runner.STAGES, "analyze", boom)
    runner.claim_next_job("w")
    assert runner.run_job(settings, job_id, threading.Event()) == "failed"
    st = stages(job_id)
    assert st["detect_events"] == "succeeded" and st["analyze"] == "failed" and st["rank"] == "pending"
    with session_scope() as s:
        job = s.get(Job, job_id)
        assert job.error["code"] == "media_error" and job.error["stage"] == "analyze"
        assert any(log.level == "error" for log in s.query(JobLog).filter_by(job_id=job_id))

    monkeypatch.setitem(runner.STAGES, "analyze", lambda ctx: calls.append("analyze"))
    calls.clear()
    with session_scope() as s:
        runner.reset_for_retry(s, s.get(Job, job_id))
    runner.claim_next_job("w")
    assert runner.run_job(settings, job_id, threading.Event()) == "succeeded"
    assert calls == ["analyze", "rank", "refine", "render", "validate"]  # completed stages were not repeated


def test_transient_failure_is_retried_once_automatically(settings, job_id, calls, monkeypatch):
    attempts = []

    def flaky(ctx):
        attempts.append(1)
        if len(attempts) == 1:
            raise SubprocessFailed("ffmpeg exited with code 1")

    monkeypatch.setitem(runner.STAGES, "extract_audio", flaky)
    runner.claim_next_job("w")
    assert runner.run_job(settings, job_id, threading.Event()) == "succeeded" and len(attempts) == 2


def test_cancellation_stops_at_the_next_checkpoint(settings, job_id, calls, monkeypatch):
    def cancel_then_checkpoint(ctx):
        with session_scope() as s:
            s.get(Job, job_id).cancel_requested = True
        ctx._last_cancel_check = 0
        ctx.should_stop()

    monkeypatch.setitem(runner.STAGES, "transcribe", cancel_then_checkpoint)
    runner.claim_next_job("w")
    assert runner.run_job(settings, job_id, threading.Event()) == "cancelled"
    st = stages(job_id)
    assert st["transcribe"] == "cancelled" and st["detect_events"] == "pending"


def test_shutdown_requeues_instead_of_failing(settings, job_id, calls, monkeypatch):
    shutdown = threading.Event()

    def stop_worker(ctx):
        shutdown.set()
        ctx.should_stop()

    monkeypatch.setitem(runner.STAGES, "detect_events", stop_worker)
    runner.claim_next_job("w")
    assert runner.run_job(settings, job_id, shutdown) == "queued"
    st = stages(job_id)
    assert status(job_id) == "queued" and st["transcribe"] == "succeeded" and st["detect_events"] == "pending"


def test_crashed_worker_jobs_are_recovered_not_marked_done(settings, job_id, calls):
    runner.claim_next_job("dead-worker")
    with session_scope() as s:
        job = s.get(Job, job_id)
        job.heartbeat_at = now() - timedelta(minutes=5)
        job.stages[0].status = "succeeded"
        job.stages[1].status = "running"
    assert runner.recover_interrupted_jobs() == 1
    assert status(job_id) == "queued"
    st = stages(job_id)
    assert st["import"] == "succeeded" and st["extract_audio"] == "pending"
    assert runner.recover_interrupted_jobs() == 0


def test_a_live_job_is_not_stolen(settings, job_id):
    runner.claim_next_job("alive")
    assert runner.recover_interrupted_jobs() == 0 and status(job_id) == "running"


def test_rerun_from_a_stage_keeps_upstream_results(settings, job_id, calls):
    runner.claim_next_job("w")
    runner.run_job(settings, job_id, threading.Event())
    calls.clear()
    with session_scope() as s:
        runner.reset_for_retry(s, s.get(Job, job_id), "rank")
    assert stages(job_id)["analyze"] == "succeeded" and stages(job_id)["rank"] == "pending"
    runner.claim_next_job("w")
    runner.run_job(settings, job_id, threading.Event())
    assert calls == ["rank", "refine", "render", "validate"]


def test_unexpected_exception_ends_in_a_recorded_failure(settings, job_id, calls, monkeypatch):
    monkeypatch.setitem(runner.STAGES, "import", lambda ctx: 1 / 0)
    runner.claim_next_job("w")
    assert runner.run_job(settings, job_id, threading.Event()) == "failed"
    with session_scope() as s:
        assert "ZeroDivisionError" in s.get(Job, job_id).error["message"]


def test_priority_order(settings, job_id):
    with session_scope() as s:
        project_id = s.get(Job, job_id).project_id
        urgent = runner.create_job(s, project_id=project_id, kind="render", priority=20, payload={"clip_id": "x", "mode": "preview"}).id
    assert runner.claim_next_job("w") == urgent
