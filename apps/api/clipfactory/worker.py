"""The background worker: ``python -m clipfactory.worker``.

A plain polling loop over the SQLite job table. The queue interface is three
functions (claim, run, recover) so a Redis-backed queue can replace the polling
later without touching the stages.
"""

from __future__ import annotations

import os
import signal
import socket
import threading
import time

from clipfactory.config import get_settings
from clipfactory.db import migrate, session_scope
from clipfactory.logs import get_logger, register_secret, setup_logging
from clipfactory.models import AppSetting
from clipfactory.pipeline.runner import claim_next_job, recover_interrupted_jobs, run_job

log = get_logger("worker")
POLL_SECONDS = 1.0
RECOVERY_SECONDS = 15.0


def _beat(worker_id: str, stop: threading.Event) -> None:
    """Liveness signal for the dashboard ("is the worker running?"), independent of any job."""
    while True:
        try:
            with session_scope() as s:
                row = s.get(AppSetting, "worker_heartbeat")
                if row:
                    row.value = {"worker": worker_id, "n": int(row.value.get("n", 0)) + 1}
                else:
                    s.add(AppSetting(key="worker_heartbeat", value={"worker": worker_id, "n": 0}))
        except Exception:  # noqa: BLE001
            log.exception("worker heartbeat failed")
        if stop.wait(5.0):
            return


def main() -> None:
    settings = get_settings()
    setup_logging(settings.clipfactory_log_level)
    if settings.openrouter_api_key:
        register_secret(settings.openrouter_api_key.get_secret_value())
    migrate()
    shutdown = threading.Event()

    def stop(signum, _frame) -> None:  # noqa: ANN001
        log.info("signal %s received — finishing up", signum)
        shutdown.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    threading.Thread(target=_beat, args=(worker_id, shutdown), daemon=True).start()
    recovered = recover_interrupted_jobs()
    log.info("worker started", extra={"data": {"worker": worker_id, "recovered_jobs": recovered, "test_mode": settings.test_mode,
                                               "openrouter_key": settings.has_openrouter_key}})  # fmt: skip
    last_recovery = time.monotonic()
    while not shutdown.is_set():
        job_id = claim_next_job(worker_id)
        if job_id is None:
            # A job whose worker died a few seconds before this one started still looks alive at startup,
            # so the sweep for stale heartbeats is repeated while idle, not done just once.
            if time.monotonic() - last_recovery >= RECOVERY_SECONDS:
                last_recovery = time.monotonic()
                if recovered := recover_interrupted_jobs():
                    log.info("recovered interrupted jobs", extra={"data": {"count": recovered}})
            shutdown.wait(POLL_SECONDS)
            continue
        log.info("job claimed", extra={"data": {"job": job_id}})
        status = run_job(settings, job_id, shutdown)
        log.info("job finished", extra={"data": {"job": job_id, "status": status}})
    log.info("worker stopped")


if __name__ == "__main__":
    main()
