"""Records one live Twitch channel into short segments, and finds the segments covering a moment.

``streamlink`` handles Twitch (waits for the channel to go live, skips ad segments, low-latency
playlist) and pipes into an FFmpeg segmenter. A segment is dated by when it finished arriving (its
modification time), so "what was on screen when chat went wild" is a lookup by time. Arrival time is
the timeline on purpose: the recorder receives the stream when viewers do, and ad breaks removed
by streamlink are simply gaps.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from clipfactory.errors import ClipFactoryError
from clipfactory.logs import get_logger
from clipfactory.media import ffmpeg
from clipfactory.media.ingest import twitch_channel_url
from clipfactory.media.proc import require, terminate

log = get_logger("recorder")

SEGMENT_S = 10
STREAM_QUALITY = "1080p60,1080p,720p60,720p,best"
# streamlink ends by itself when a stream stops sending data; this is only the backstop for a hung
# process. It must stay well above the longest ad break, during which no segment is written.
_STALL_S = 600
_TRUSTED_AFTER_S = 30  # a recorder that failed is shown as failing until a new one has stayed up this long
_SAME_BROADCAST_S = 120  # footage resuming this soon after it stopped is the same broadcast, not a new one
RecorderState = Literal["recording", "offline", "error"]


@dataclass(frozen=True)
class Segment:
    path: Path
    start: float  # epoch seconds: when the previous segment finished arriving
    end: float  # when this one did (its modification time)


def _files(session_dir: Path) -> list[Path]:
    return sorted((f for f in session_dir.glob("*.ts") if f.stem.isdigit()), key=lambda f: int(f.stem))


def list_segments(session_dir: Path, *, closed: bool) -> list[Segment]:
    """Complete segments of one recording session, oldest first.

    While the session is being recorded (``closed=False``) the newest file is still being written
    and is left out.
    """
    files = _files(session_dir)
    if not closed:
        files = files[:-1]
    ends = [f.stat().st_mtime for f in files]
    starts = [ends[0] - SEGMENT_S, *ends[:-1]] if ends else []
    return [Segment(f, a, b) for f, a, b in zip(files, starts, ends, strict=True)]


def _sessions(channel_dir: Path) -> list[Path]:
    return [d for d in channel_dir.iterdir() if d.is_dir()] if channel_dir.exists() else []


def window_segments(channel_dir: Path, start: float, end: float, live_session: Path | None) -> tuple[list[Segment], bool]:
    """Segments of the single session that covers most of [start, end], and whether footage for it may still arrive.

    Segments of different sessions are never mixed: each session restarts its timestamps.
    """
    best: list[Segment] = []
    best_overlap = 0.0
    more = False
    for session in _sessions(channel_dir):
        segments = list_segments(session, closed=session != live_session)
        if session == live_session:
            more = not segments or segments[-1].end < end
        inside = [s for s in segments if s.end >= start and s.start <= end]
        overlap = sum(min(s.end, end) - max(s.start, start) for s in inside)
        if overlap > best_overlap:
            best, best_overlap = inside, overlap
    return best, more


def prune(channel_dir: Path, *, before: float, live_session: Path | None) -> int:
    """Delete complete segments that ended before ``before`` and drop emptied finished sessions. Returns bytes freed."""
    freed = 0
    for session in _sessions(channel_dir):
        for segment in list_segments(session, closed=session != live_session):
            if segment.end < before:
                freed += segment.path.stat().st_size
                segment.path.unlink()
        if session != live_session and not any(session.iterdir()):
            session.rmdir()
    return freed


class Recorder:
    """Keeps one channel recorded for as long as it is asked to. ``poll`` is called every tick and does
    everything: the first start, the restart after a stream ends, the retries after a failure."""

    def __init__(self, login: str, channel_dir: Path) -> None:
        self.login = login
        self.channel_dir = channel_dir
        self.session_dir: Path | None = None  # the session being recorded, if any
        self.recording_since: float | None = None  # start of the current broadcast, kept across a short interruption
        self.detail = ""
        self._procs: list[subprocess.Popen] = []
        self._log = None
        self._retry_at: float | None = 0.0  # when to (re)start; None once stopped
        self._started_at = 0.0
        self._failures = 0
        self._last_footage: tuple[float, float] | None = None  # (when the previous session ended, its recording_since)

    def _source_command(self) -> list[str]:
        # No credentials, no ad-avoidance tricks: ad segments are skipped by streamlink, which leaves a gap.
        return [sys.executable, "-m", "streamlink_cli", "--stdout", "--loglevel", "info", "--retry-streams", "15",
                "--twitch-low-latency", twitch_channel_url(self.login), STREAM_QUALITY]  # fmt: skip

    def _spawn(self) -> list[subprocess.Popen]:
        """streamlink piped into the segmenter. If the segmenter cannot start, streamlink is not left running."""
        require("ffmpeg")
        source = subprocess.Popen(self._source_command(), stdout=subprocess.PIPE, stderr=self._log)
        assert source.stdout
        try:
            sink = subprocess.Popen(ffmpeg.segment_command(self.session_dir, SEGMENT_S), stdin=source.stdout, stderr=self._log)
        except OSError:
            terminate(source)
            raise
        finally:
            source.stdout.close()  # the segmenter holds the only read end: it sees EOF when streamlink exits
        return [source, sink]

    def _start(self, now: float) -> None:
        self.session_dir = self.channel_dir / str(int(now))
        self._started_at = now
        try:
            self.session_dir.mkdir(parents=True, exist_ok=True)
            self._log = (self.channel_dir / "recorder.log").open("wb")
            self._procs = self._spawn()
        except (OSError, ClipFactoryError) as exc:
            self._close()
            self.detail = str(exc)[:300]
            self._failed(now)

    def _failed(self, now: float) -> None:
        self._failures += 1
        self._retry_at = now + min(60, 5 * 2**self._failures)
        log.warning("recorder for %s is not recording: %s", self.login, self.detail)

    def poll(self, now: float) -> RecorderState:
        if not self._procs:
            if self._retry_at is not None and now >= self._retry_at:
                self._retry_at = None
                self._start(now)
            return "error" if self._failures else "offline"
        newest = next(reversed(_files(self.session_dir)), None)
        if any(p.poll() is not None for p in self._procs):
            # The stream ended (normal) or the recorder died before recording anything (network, Twitch, a bad build).
            recorded = newest is not None and newest.stat().st_size > 0
            if recorded and self.recording_since is not None:
                self._last_footage = (now, self.recording_since)
            self._close()
            if recorded:
                self._failures, self._retry_at = 0, now + 5
            else:
                self._failed(now)
            return "error" if self._failures else "offline"
        if newest is None:
            # streamlink is waiting for the channel to go live — unless it only just replaced one that failed.
            if self._failures and now - self._started_at >= _TRUSTED_AFTER_S:
                self._failures = 0
            return "error" if self._failures else "offline"
        self._failures = 0
        if self.recording_since is None:
            resumed = self._last_footage is not None and now - self._last_footage[0] < _SAME_BROADCAST_S
            self.recording_since = self._last_footage[1] if resumed else now
        if now - newest.stat().st_mtime > _STALL_S:
            log.warning("recorder for %s stalled; restarting", self.login)
            self._close()
            self._retry_at = now + 5
            return "offline"
        return "recording"

    def stop(self) -> None:
        self._close()
        self._retry_at = None

    def _close(self) -> None:
        """End the session: stop streamlink, let the segmenter finish its last file, keep the last log line."""
        for proc in self._procs:
            terminate(proc)
        self._procs = []
        self.session_dir = None
        self.recording_since = None
        if self._log is not None:
            self._log.close()
            self._log = None
            lines = (self.channel_dir / "recorder.log").read_text(errors="replace").strip().splitlines()
            self.detail = lines[-1][-300:] if lines else ""
