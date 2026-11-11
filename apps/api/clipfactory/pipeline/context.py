"""What a running stage is given: logging, progress, cancellation and metrics, all persisted."""

from __future__ import annotations

import threading
import time
from typing import Any

from clipfactory.config import Settings
from clipfactory.db import session_scope
from clipfactory.errors import JobCancelled, WorkerShutdown
from clipfactory.logs import get_logger, redact
from clipfactory.models import Job, JobLog, JobStage
from clipfactory.settings_store import RuntimeSettings

log = get_logger("job")


class JobContext:
    def __init__(self, settings: Settings, runtime: RuntimeSettings, job_id: str, project_id: str, shutdown: threading.Event) -> None:
        self.settings = settings
        self.runtime = runtime
        self.job_id = job_id
        self.project_id = project_id
        self.stage = ""
        self._shutdown = shutdown
        self._last_progress_write = 0.0
        self._last_progress: float | None = None
        self._last_cancel_check = 0.0
        self._cancelled = False
        self._lock = threading.Lock()

    # ── logging ─────────────────────────────────────────────────────────────

    def log(self, message: str, level: str = "info", **data: Any) -> None:
        message = redact(message)
        log.log({"info": 20, "warning": 30, "error": 40}.get(level, 20), message, extra={"data": {"job": self.job_id, "stage": self.stage, **data}})
        with session_scope() as s:
            s.add(JobLog(job_id=self.job_id, stage=self.stage or None, level=level, message=message[:2000], data=data or None))

    def warn(self, message: str) -> None:
        """A problem the job survived. Shown on the job, not just buried in the log."""
        self.log(message, "warning")
        with session_scope() as s:
            job = s.get(Job, self.job_id)
            if job is not None and message not in (job.warnings or []):
                job.warnings = [*(job.warnings or []), redact(message)[:500]]

    # ── progress ────────────────────────────────────────────────────────────

    def progress(self, fraction: float | None, detail: str | None = None) -> None:
        """Report measured progress (0–1), or None when it cannot be measured. Writes are throttled."""
        with self._lock:
            now = time.monotonic()
            changed = fraction is None or self._last_progress is None or abs(fraction - self._last_progress) >= 0.01
            if now - self._last_progress_write < 0.7 and not (fraction is not None and fraction >= 1.0):
                return
            if not changed and now - self._last_progress_write < 5:
                return
            self._last_progress_write, self._last_progress = now, fraction
        with session_scope() as s:
            stage = s.query(JobStage).filter_by(job_id=self.job_id, name=self.stage).one_or_none()
            if stage is not None:
                stage.progress = None if fraction is None else max(0.0, min(1.0, fraction))
                if detail is not None:
                    stage.detail = detail[:300]

    def metrics(self, **values: Any) -> None:
        with session_scope() as s:
            stage = s.query(JobStage).filter_by(job_id=self.job_id, name=self.stage).one_or_none()
            if stage is not None:
                stage.metrics = {**(stage.metrics or {}), **values}

    def cache_hit(self, what: str) -> None:
        self.log(f"réutilisation du cache : {what}")
        with session_scope() as s:
            stage = s.query(JobStage).filter_by(job_id=self.job_id, name=self.stage).one_or_none()
            if stage is not None:
                stage.cache_hit = True

    # ── cancellation ────────────────────────────────────────────────────────

    def should_stop(self) -> None:
        """Raise if the worker is shutting down or the user cancelled. Cheap enough to call in loops."""
        if self._shutdown.is_set():
            raise WorkerShutdown("worker is shutting down")
        now = time.monotonic()
        if not self._cancelled and now - self._last_cancel_check >= 1.0:
            self._last_cancel_check = now
            with session_scope() as s:
                job = s.get(Job, self.job_id)
                self._cancelled = job is None or job.cancel_requested
        if self._cancelled:
            raise JobCancelled("cancelled by the user")
