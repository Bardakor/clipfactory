"""Subprocess execution with timeouts, cancellation and bounded error output."""

from __future__ import annotations

import shutil
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable

from clipfactory.errors import DependencyMissing, JobCancelled, StageTimeout, SubprocessFailed
from clipfactory.logs import get_logger, redact

log = get_logger("proc")

ShouldStop = Callable[[], None]  # raises JobCancelled / WorkerShutdown to abort


def require(binary: str) -> str:
    path = shutil.which(binary)
    if not path:
        raise DependencyMissing(f"{binary} is not installed or not on PATH", binary=binary)
    return path


def _terminate(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def run(
    cmd: list[str],
    *,
    timeout: float = 600,
    should_stop: ShouldStop | None = None,
    on_line: Callable[[str], None] | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> str:
    """Run ``cmd`` to completion and return stdout.

    stdout is streamed line by line to ``on_line`` (used for real progress
    reporting); stderr is kept as a bounded tail for error messages.
    """
    require(cmd[0]) if "/" not in cmd[0] else None
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace", cwd=cwd, env=env
    )
    out: list[str] = []
    err: deque[str] = deque(maxlen=40)

    def _pump_out() -> None:
        assert proc.stdout
        for line in proc.stdout:
            line = line.rstrip("\n")
            if on_line:
                try:
                    on_line(line)
                except Exception:  # noqa: BLE001 - a progress callback must never kill the process
                    log.exception("on_line callback failed")
            else:
                out.append(line)

    def _pump_err() -> None:
        assert proc.stderr
        for line in proc.stderr:
            err.append(line.rstrip("\n"))

    threads = [threading.Thread(target=f, daemon=True) for f in (_pump_out, _pump_err)]
    for t in threads:
        t.start()
    deadline = time.monotonic() + timeout
    try:
        while proc.poll() is None:
            if should_stop:
                should_stop()
            if time.monotonic() > deadline:
                raise StageTimeout(f"{cmd[0]} exceeded {timeout:.0f}s", command=cmd[0])
            time.sleep(0.1)
    except BaseException:
        _terminate(proc)
        raise
    finally:
        for t in threads:
            t.join(timeout=5)
    if proc.returncode != 0:
        raise SubprocessFailed(
            f"{cmd[0]} exited with code {proc.returncode}",
            command=cmd[0],
            returncode=proc.returncode,
            stderr=redact("\n".join(err))[-2000:],
        )
    return "\n".join(out)


__all__ = ["run", "require", "JobCancelled", "ShouldStop"]
