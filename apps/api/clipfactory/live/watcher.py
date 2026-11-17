"""The live watcher: ``python -m clipfactory.live.watcher``.

Stays plugged into every enabled, authorised channel: records it while it is live, counts who is
talking in chat, and when chat is abnormally busy turns the footage around that moment into a
source, a project and an ordinary pipeline job. It makes no clip itself — the worker does, with the
same stages as for any other source.
"""

from __future__ import annotations

import fcntl
import os
import shutil
import signal
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import IO

import psutil
from sqlalchemy import select

from clipfactory.analysis.chat import BUCKET_S, Spike, SpikeDetector
from clipfactory.config import Settings, get_settings
from clipfactory.db import migrate, session_scope
from clipfactory.errors import ClipFactoryError, Conflict, IngestError, NotFound
from clipfactory.live.chat import ChatClient, ChatCounter
from clipfactory.live.recorder import SEGMENT_S, Recorder, prune, window_segments
from clipfactory.logs import get_logger, setup_logging
from clipfactory.media import ffmpeg, ingest
from clipfactory.models import AppSetting, LiveChannel, LiveMoment, Project, Source, new_id
from clipfactory.pipeline.runner import create_job
from clipfactory.settings_store import LiveSettings, load_runtime
from clipfactory.timeutil import from_epoch, to_epoch

log = get_logger("live")

HEARTBEAT_KEY = "live_heartbeat"
_WARMUP_S = 120  # no spike this soon after a recording starts: the chat of a stream that just began is all greetings
_GRACE_S = 45  # how long after a window's end its footage is still waited for
_MIN_FOOTAGE_S = 10
NO_FOOTAGE = "Aucune image exploitable n'a été enregistrée autour de ce pic de chat."
_SERIES = 60  # buckets kept for the dashboard chart: ten minutes
_LIVE_PRIORITY = 10  # a live moment is worth more now than in an hour: it goes ahead of queued VOD work


@dataclass
class _Watch:
    login: str
    recorder: Recorder
    detector: SpikeDetector = field(default_factory=SpikeDetector)
    series: deque[tuple[int, int | None]] = field(default_factory=lambda: deque(maxlen=_SERIES))  # (bucket start, chatters or unknown)


def materialize(settings: Settings, moment_id: str, parts: list[Path]) -> None:
    """Turn the recorded segments of a moment into a ready source, a project and a queued pipeline job.

    Raises ``IngestError`` when the segments hold too little footage to make a clip. Whatever goes wrong,
    no file is left in ``data/sources``.
    """
    source_id = new_id()
    path = settings.path("sources", f"{source_id}.mp4")
    try:
        probe = ffmpeg.probe(ffmpeg.concat_copy(parts, path))
        if probe.duration_ms < _MIN_FOOTAGE_S * 1000:
            raise IngestError("too little footage was recorded around this moment", duration_ms=probe.duration_ms)
        with session_scope() as s:
            moment = s.get(LiveMoment, moment_id)
            if moment is None:  # its channel was removed while the footage was being assembled
                raise NotFound("this moment no longer exists")
            channel = moment.channel
            when = datetime.fromtimestamp(to_epoch(moment.peak_at))  # local time, for a title a person reads
            source = Source(id=source_id, kind="twitch", title=f"{channel.login} — direct du {when:%d/%m à %H:%M}", channel=channel.login,
                            origin_url=ingest.twitch_channel_url(channel.login), rights_confirmed=channel.rights_confirmed)  # fmt: skip
            s.add(source)
            s.flush()
            source = ingest.finalize_source(s, settings, source, path, probe)
            project = Project(source_id=source.id, name=source.title, settings=channel.settings, is_test=settings.test_mode)
            s.add(project)
            s.flush()
            create_job(s, project_id=project.id, kind="pipeline", priority=_LIVE_PRIORITY, is_test=settings.test_mode)
            moment.status, moment.project_id = "queued", project.id
    except Exception:
        path.unlink(missing_ok=True)
        raise


class Watcher:
    def __init__(self, settings: Settings, *, chat: ChatClient, counter: ChatCounter,
                 recorder_factory: Callable[[str, Path], Recorder] = Recorder) -> None:  # fmt: skip
        self._settings = settings
        self._chat = chat
        self._counter = counter
        self._recorder_factory = recorder_factory
        self._watch: dict[str, _Watch] = {}  # channel id → what we run for it
        self._known: set[str] | None = None
        self._stopped: set[str] = set()
        self._next_bucket: int | None = None
        self._beats = 0
        self._last_beat = float("-inf")

    def tick(self, now: float) -> None:
        with session_scope() as s:
            live = load_runtime(s).live
            channels = {c.id: c.login for c in s.scalars(
                select(LiveChannel).where(LiveChannel.enabled.is_(True), LiveChannel.rights_confirmed.is_(True)))}  # fmt: skip
            pending = [(m.id, m.channel_id, to_epoch(m.window_start), to_epoch(m.window_end))
                       for m in s.scalars(select(LiveMoment).where(LiveMoment.status == "pending"))]  # fmt: skip
        self._reconcile(channels)
        states = {cid: self._poll(watch, now) for cid, watch in self._watch.items()}
        bucket_closed = self._next_bucket is not None and self._next_bucket + BUCKET_S <= now
        spikes = self._close_buckets(now, live, states)
        # What was just observed is written before anything slow or fallible: a spike must survive this tick.
        self._persist(now, live, states, spikes)
        self._fail(self._materialize_ready(now, pending))
        if bucket_closed:
            windows = [(cid, start) for _, cid, start, _ in pending] + [(cid, spike.t0 - live.before_s) for cid, spike in spikes]
            self._prune(now, live, windows)

    @property
    def watching(self) -> bool:
        return bool(self._watch)

    def close(self) -> None:
        for watch in self._watch.values():
            watch.recorder.stop()

    def _poll(self, watch: _Watch, now: float) -> str:
        try:
            return watch.recorder.poll(now)
        except Exception as exc:  # noqa: BLE001 - one channel's recorder must not stop the others
            log.exception("recorder for %s failed", watch.login)
            watch.recorder.detail = str(exc)[:300]
            return "error"

    def _reconcile(self, channels: dict[str, str]) -> None:
        """Run a recorder and listen to chat for exactly the enabled channels; drop the footage of the others."""
        if channels.keys() == self._known:
            return
        self._known = set(channels)
        for cid in self._watch.keys() - channels.keys():
            self._watch.pop(cid).recorder.stop()
            self._stopped.add(cid)
        root = self._settings.path("live")
        for folder in root.iterdir():
            if folder.is_dir() and folder.name not in channels:
                shutil.rmtree(folder, ignore_errors=True)
        for cid in channels.keys() - self._watch.keys():  # the recorder starts itself at its first poll
            self._watch[cid] = _Watch(channels[cid], self._recorder_factory(channels[cid], self._settings.path("live", cid)))
        self._chat.set_channels({watch.login for watch in self._watch.values()})

    def _close_buckets(self, now: float, live: LiveSettings, states: dict[str, str]) -> list[tuple[str, Spike]]:
        current = int(now) // BUCKET_S * BUCKET_S
        if self._next_bucket is None or current - self._next_bucket > 6 * BUCKET_S:
            # First tick, or the machine slept: buckets nobody watched are unknown, not empty.
            for watch in self._watch.values():
                watch.series.clear()
            self._next_bucket = current
        spikes = []
        while self._next_bucket + BUCKET_S <= now:
            t0 = self._next_bucket
            self._next_bucket += BUCKET_S
            for cid, watch in self._watch.items():
                chatters = self._counter.take(watch.login, t0)
                if not self._chat.covered(watch.login, t0):
                    watch.series.append((t0, None))  # chat was not being read: say so rather than count a silence
                    continue
                since = watch.recorder.recording_since
                armed = states[cid] == "recording" and since is not None and t0 - since >= _WARMUP_S
                spike = watch.detector.push(t0, chatters, spike_z=live.spike_z, min_chatters=live.min_chatters,
                                            refractory_s=live.before_s + live.after_s, armed=armed)  # fmt: skip
                watch.series.append((t0, chatters))
                if spike:
                    spikes.append((cid, spike))
        return spikes

    def _materialize_ready(self, now: float, pending: list[tuple[str, str, float, float]]) -> dict[str, str]:
        """Queue every pending moment whose footage is complete (or will not get any better). Returns the failures."""
        failures: dict[str, str] = {}
        for moment_id, cid, start, end in pending:
            watch = self._watch.get(cid)
            segments, more = window_segments(self._settings.path("live", cid), start, end, watch.recorder.session_dir if watch else None)
            if more and now < end + _GRACE_S:
                continue
            if not segments:
                failures[moment_id] = NO_FOOTAGE
                continue
            try:
                materialize(self._settings, moment_id, [s.path for s in segments])
            except IngestError:
                failures[moment_id] = NO_FOOTAGE
            except ClipFactoryError as exc:
                log.warning("moment %s could not be assembled: %s", moment_id, exc.message)
                failures[moment_id] = f"Assemblage des images impossible : {exc.message}"[:300]
            except Exception as exc:  # noqa: BLE001 - one moment must not take the watcher (and the other channels) down
                log.exception("moment %s could not be assembled", moment_id)
                failures[moment_id] = f"Assemblage des images impossible : erreur interne ({type(exc).__name__})."
        return failures

    def _prune(self, now: float, live: LiveSettings, windows: list[tuple[str, float]]) -> None:
        """Drop footage older than the buffer, except what a moment not yet assembled still needs."""
        for cid, watch in self._watch.items():
            keep_from = min([now - live.buffer_min * 60, *(start - SEGMENT_S for c, start in windows if c == cid)])
            prune(self._settings.path("live", cid), before=keep_from, live_session=watch.recorder.session_dir)

    def _fail(self, failures: dict[str, str]) -> None:
        if not failures:
            return
        with session_scope() as s:
            for moment_id, detail in failures.items():
                moment = s.get(LiveMoment, moment_id)
                if moment is not None:
                    moment.status, moment.detail = "failed", detail

    def _persist(self, now: float, live: LiveSettings, states: dict[str, str], spikes: list[tuple[str, Spike]]) -> None:
        with session_scope() as s:
            for cid, watch in self._watch.items():
                channel = s.get(LiveChannel, cid)
                if channel is None:
                    continue
                since = watch.recorder.recording_since
                channel.state = states[cid]
                channel.state_detail = watch.recorder.detail[:300] if states[cid] == "error" else None
                channel.live_since = from_epoch(since) if states[cid] == "recording" and since is not None else None
                if watch.series:
                    channel.chat = {"t0": watch.series[0][0], "chatters": [value for _, value in watch.series]}
            for cid in self._stopped:
                channel = s.get(LiveChannel, cid)
                if channel is not None:
                    channel.state, channel.state_detail, channel.live_since, channel.chat = "offline", None, None, None
            self._stopped.clear()
            for cid, spike in spikes:
                s.add(LiveMoment(channel_id=cid, peak_at=from_epoch(spike.t0), chatters=spike.chatters, baseline=spike.baseline, z=spike.z,
                                 window_start=from_epoch(spike.t0 - live.before_s), window_end=from_epoch(spike.t0 + live.after_s)))  # fmt: skip
            if now - self._last_beat >= 5:
                self._last_beat, self._beats = now, self._beats + 1
                row = s.get(AppSetting, HEARTBEAT_KEY)
                if row:
                    row.value = {"n": self._beats}
                else:
                    s.add(AppSetting(key=HEARTBEAT_KEY, value={"n": self._beats}))
        for cid, spike in spikes:
            log.info("chat spike on %s: %d chatters against a baseline of %.0f", self._watch[cid].login, spike.chatters, spike.baseline)


def _reap_orphans(root: Path) -> None:
    """Stop segmenters left by a watcher that was killed: they would keep filling a folder nobody reads.

    streamlink, which feeds them, exits by itself when its pipe closes.
    """
    prefix = str(root) + os.sep
    for proc in psutil.process_iter(["name", "cmdline"]):
        if "ffmpeg" in (proc.info["name"] or "") and any(arg.startswith(prefix) for arg in proc.info["cmdline"] or []):
            log.warning("stopping an orphaned recorder (pid %d)", proc.pid)
            try:
                proc.terminate()
            except psutil.Error:  # it exited on its own in the meantime
                pass


def single_instance(settings: Settings) -> IO[str]:
    """Take this data folder's watcher lock, held until the returned file is closed (or the process ends).

    Two watchers on one folder would stop each other's recorders and both clip every spike.
    """
    handle = (settings.path("live") / ".watcher.lock").open("w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise Conflict("another live watcher is already running on this data folder") from exc
    return handle


def keep_awake(holder: subprocess.Popen | None, wanted: bool) -> subprocess.Popen | None:
    """Keep the Mac from idle-sleeping for as long as a channel is watched: a sleeping machine records nothing.

    ``caffeinate -i -w <pid>`` holds the assertion and lets go by itself if this process dies. A no-op where
    ``caffeinate`` does not exist. Closing the lid still sleeps the machine.
    """
    if wanted and holder is None and (binary := shutil.which("caffeinate")):
        return subprocess.Popen([binary, "-i", "-w", str(os.getpid())])
    if not wanted and holder is not None:
        holder.terminate()
        return None
    return holder


def main() -> None:
    settings = get_settings()
    setup_logging(settings.clipfactory_log_level)
    migrate()
    shutdown = threading.Event()

    def stop(signum, _frame) -> None:  # noqa: ANN001
        log.info("signal %s received — stopping the recorders", signum)
        shutdown.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    try:
        lock = single_instance(settings)
    except Conflict as exc:
        log.error("%s — this one exits", exc.message)
        raise SystemExit(1) from exc
    _reap_orphans(settings.path("live"))
    counter = ChatCounter()
    chat = ChatClient(counter, shutdown)
    chat.start()
    watcher = Watcher(settings, chat=chat, counter=counter)
    awake = None
    log.info("live watcher started")
    try:
        while not shutdown.is_set():
            try:
                watcher.tick(time.time())
            except Exception:  # noqa: BLE001 - one bad tick must not end a session that runs for hours
                log.exception("live tick failed")
            awake = keep_awake(awake, watcher.watching)
            shutdown.wait(1.0)
    finally:
        watcher.close()
        keep_awake(awake, False)
        lock.close()
    log.info("live watcher stopped")


if __name__ == "__main__":
    main()
