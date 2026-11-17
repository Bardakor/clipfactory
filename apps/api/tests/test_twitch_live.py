"""Live Twitch clipping: chat spikes, chat reader, recorder, watcher. No network: sockets are local, media is synthetic."""

from __future__ import annotations

import os
import random
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from clipfactory import config, db
from clipfactory.ai.analyst import FakeAnalyst
from clipfactory.ai.schemas import Critique
from clipfactory.analysis.chat import MIN_HISTORY, SpikeDetector, spike_score
from clipfactory.db import session_scope
from clipfactory.domain import live_project_settings
from clipfactory.errors import ClipFactoryError, Conflict, ValidationFailed
from clipfactory.live import chat as chat_module
from clipfactory.live import recorder
from clipfactory.live import watcher as watcher_module
from clipfactory.live.chat import ChatClient, ChatCounter, parse_privmsg
from clipfactory.live.recorder import Recorder, list_segments, prune, window_segments
from clipfactory.live.watcher import HEARTBEAT_KEY, NO_FOOTAGE, Watcher, keep_awake, materialize, single_instance
from clipfactory.media import ffmpeg, ingest
from clipfactory.models import AppSetting, Candidate, Clip, Job, JobLog, LiveChannel, LiveMoment, Project, Source
from clipfactory.pipeline.runner import claim_next_job, run_job
from clipfactory.pipeline.stages_editorial import credit_social
from clipfactory.settings_store import RuntimeSettings
from clipfactory.timeutil import from_epoch, now, to_epoch

DEFAULTS = {"spike_z": 3.5, "min_chatters": 5, "refractory_s": 60, "armed": True}


def _feed(detector: SpikeDetector, values: list[int], start: int = 0, **overrides) -> list:
    return [detector.push(start + i * 10, v, **{**DEFAULTS, **overrides}) for i, v in enumerate(values)]


# ── spike detection ─────────────────────────────────────────────────────────


def test_busy_but_steady_chat_is_not_a_spike():
    rng = random.Random(7)
    spikes = _feed(SpikeDetector(), [rng.randint(35, 45) for _ in range(200)])
    assert not any(spikes)


def test_sudden_jump_is_a_spike():
    spikes = _feed(SpikeDetector(), [8, 7, 9, 8, 8, 7, 9, 8, 60])
    assert spikes[:-1] == [None] * 8
    spike = spikes[-1]
    assert spike.t0 == 80 and spike.chatters == 60 and spike.baseline == 8 and spike.z >= 3.5


def test_small_chat_needs_real_people():
    detector = SpikeDetector()
    quiet = [0, 1, 2, 1, 0, 1, 2, 1]
    assert not any(_feed(detector, [*quiet, 4]))  # above its baseline, but four people are not a moment
    assert _feed(detector, [1, 9], start=90)[-1] is not None


def test_no_decision_without_history():
    assert spike_score([3] * (MIN_HISTORY - 1), 50) is None
    assert spike_score([3] * MIN_HISTORY, 50) is not None
    assert not any(_feed(SpikeDetector(), [3, 3, 3, 3, 50]))


def test_spike_fires_once_then_respects_refractory():
    detector = SpikeDetector()
    calm = [8, 7, 9, 8, 8, 7, 9, 8]
    first = _feed(detector, [*calm, 60, 70, 65])  # one moment, three hot buckets
    assert [bool(s) for s in first[-3:]] == [True, False, False]
    again = _feed(detector, [8, 60], start=110)  # rises again 40 s after the spike: still inside the refractory minute
    assert not any(again)
    later = _feed(detector, [8, 8, 60], start=130)
    assert later[-1] is not None


def test_unarmed_detector_learns_but_does_not_fire():
    detector = SpikeDetector()
    calm = [8, 7, 9, 8, 8, 7, 9, 8]
    assert not any(_feed(detector, [*calm, 60], armed=False))
    assert _feed(detector, [62], start=90)[0] is None  # arming in the middle of a spike is not a rising edge
    assert _feed(detector, [8, 8, 8, 60], start=100)[-1] is not None


# ── chat reader ─────────────────────────────────────────────────────────────


def test_parse_privmsg():
    plain = ":alice!alice@alice.tmi.twitch.tv PRIVMSG #marlon :LETS GO"
    tagged = "@badge-info=;color=#FF0000;display-name=Alice :Alice!alice@alice.tmi.twitch.tv PRIVMSG #Marlon :he did not"
    stamped = "@badge-info=;tmi-sent-ts=1794000001234;user-id=42 :bob!bob@bob.tmi.twitch.tv PRIVMSG #marlon :KEKW"
    assert parse_privmsg(plain) == ("marlon", "alice", None)
    assert parse_privmsg(tagged) == ("marlon", "alice", None)
    assert parse_privmsg(stamped) == ("marlon", "bob", 1794000001.234)  # when Twitch says it was sent
    for other in ("PING :tmi.twitch.tv", ":tmi.twitch.tv 001 justinfan1 :Welcome, GLHF!", ":justinfan1!x@x JOIN #marlon", ""):
        assert parse_privmsg(other) is None


def test_counter_counts_distinct_chatters():
    counter = ChatCounter()
    counter.add("marlon", "alice", 100.5)
    counter.add("marlon", "alice", 103.0)  # same person twice: one chatter
    counter.add("marlon", "bob", 109.9)
    counter.add("marlon", "carol", 110.0)  # next bucket
    counter.add("other", "dave", 101.0)
    assert counter.take("marlon", 100) == 2
    assert counter.take("marlon", 100) == 0  # taken once
    assert counter.take("marlon", 110) == 1
    assert counter.take("nobody", 100) == 0

    counter.add("marlon", "erin", 104.0)  # delivered late, after its bucket was read: it must not inflate a later one
    assert counter.take("marlon", 120) == 0


class FakeIrc:
    """A local stand-in for Twitch chat: the test plays the server by hand."""

    def __init__(self) -> None:
        self.server = socket.create_server(("127.0.0.1", 0))
        self.server.settimeout(5)
        self.port = self.server.getsockname()[1]
        self.conn: socket.socket | None = None
        self._buf = b""

    def accept(self) -> None:
        self.conn, _ = self.server.accept()
        self.conn.settimeout(5)
        self._buf = b""

    def send(self, line: str) -> None:
        self.conn.sendall(f"{line}\r\n".encode())

    def expect(self, prefix: str) -> str:
        """The next line starting with ``prefix`` (anything else the client says first is skipped)."""
        while True:
            while b"\r\n" not in self._buf:
                data = self.conn.recv(4096)
                assert data, f"client hung up while the server waited for {prefix!r}"
                self._buf += data
            line, _, self._buf = self._buf.partition(b"\r\n")
            if line.decode().startswith(prefix):
                return line.decode()

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
        self.server.close()


@pytest.fixture
def irc():
    server, stop = FakeIrc(), threading.Event()
    counter = ChatCounter()
    client = ChatClient(counter, stop, host="127.0.0.1", port=server.port, tls=False)
    client.set_channels({"marlon"})
    client.start()
    yield server, client, counter
    stop.set()
    client.join(timeout=5)
    server.close()
    assert not client.is_alive()


def test_chat_client_joins_counts_and_answers_ping(irc):
    server, client, counter = irc
    server.accept()
    assert "twitch.tv/tags" in server.expect("CAP REQ ")  # tags carry the time each message was sent
    assert server.expect("NICK ").startswith("NICK justinfan")
    assert server.expect("JOIN ") == "JOIN #marlon"
    now = time.time()
    server.send(":alice!alice@alice.tmi.twitch.tv PRIVMSG #marlon :LETS GO")
    # A message Twitch sent a minute ago and that only arrives now (the link had stalled) belongs to its own minute.
    server.send(f"@tmi-sent-ts={int((now - 60) * 1000)} :bob!bob@bob.tmi.twitch.tv PRIVMSG #marlon :late")
    server.send("PING :tmi.twitch.tv")
    assert server.expect("PONG ") == "PONG :tmi.twitch.tv"  # lines are handled in order: both messages are counted by now
    bucket = int(now) // 10 * 10
    assert counter.take("marlon", int(now - 60) // 10 * 10) == 1
    assert counter.take("marlon", bucket) + counter.take("marlon", bucket + 10) == 1
    assert client.covered("marlon", int(now) + 10)
    assert not client.covered("marlon", int(now) - 100)  # was not listening back then
    assert not client.covered("someone_else", int(now) + 10)

    client.set_channels({"marlon", "pote"})
    assert server.expect("JOIN ") == "JOIN #pote"
    client.set_channels({"pote"})
    assert server.expect("PART ") == "PART #marlon"
    assert not client.covered("marlon", int(time.time()) + 10)


def test_chat_client_reconnects_and_rejoins(irc):
    server, client, _counter = irc
    server.accept()
    server.expect("JOIN ")
    before_drop = int(time.time()) + 1
    assert client.covered("marlon", before_drop)
    server.conn.close()  # Twitch drops the connection

    server.accept()  # the client comes back on its own
    assert server.expect("JOIN ") == "JOIN #marlon"
    assert not client.covered("marlon", before_drop)  # that bucket straddles the outage
    assert client.covered("marlon", int(time.time()) + 10)


def test_chat_backoff_starts_over_after_a_connection_that_held(irc, monkeypatch):
    monkeypatch.setattr(chat_module, "_HELD_S", 0.3)
    server, _client, _counter = irc
    gaps = []
    for _ in range(3):  # three good connections in a row, each dropped by Twitch after it held for a while
        server.accept()
        server.expect("JOIN ")
        time.sleep(0.4)
        server.conn.close()
        dropped = time.monotonic()
        server.accept()
        gaps.append(time.monotonic() - dropped)
        server.expect("JOIN ")
        time.sleep(0.4)
        server.conn.close()
    assert max(gaps) < 1.8, gaps  # always the first step of the backoff: an hours-long session must not end up waiting 30 s


# ── recorder ────────────────────────────────────────────────────────────────


def _session(folder: Path, ends: list[float]) -> Path:
    """A recording session as the segmenter leaves it: numbered files, each last written when its footage ended."""
    folder.mkdir(parents=True)
    for index, end in enumerate(ends):
        path = folder / f"{index:06d}.ts"
        path.write_bytes(b"x")
        os.utime(path, (end, end))
    return folder


def _real_segments(video: Path, session: Path, start: float = 1000.0) -> list[Path]:
    """The synthetic video cut into MPEG-TS segments by the recorder's own muxer, as if it had arrived from ``start``."""
    session.mkdir(parents=True)
    command = ffmpeg.segment_command(session, 5)
    subprocess.run([*command[: command.index("pipe:0")], str(video), *command[command.index("pipe:0") + 1 :]], check=True)
    parts, at = sorted(session.glob("*.ts")), start
    for part in parts:
        at += ffmpeg.probe(part).duration_ms / 1000
        os.utime(part, (at, at))
    return parts


def test_twitch_login_is_normalised_or_refused():
    for given in ("Marlon", "@Marlon", " marlon ", "https://www.twitch.tv/Marlon", "https://twitch.tv/marlon/videos", "twitch.tv/marlon?x=1"):
        assert ingest.parse_twitch_login(given) == "marlon", given
    assert ingest.twitch_channel_url("marlon") == "https://www.twitch.tv/marlon"
    for bad in ("https://evil.example/marlon", "https://www.twitch.tv/videos/2301234567", "a b", "ab", "x" * 26,
                "https://www.twitch.tv/", "marlon;rm", "http://user@twitch.tv/marlon", "twitch.tv:abc/marlon",
                "https://www.twitch.tv/popout/marlon/chat", "https://www.twitch.tv/moderator/marlon",
                "https://www.twitch.tv/settings/profile", "https://www.twitch.tv/directory", "settings",
                "https://www.twitch.tv/newpage/marlon"):  # fmt: skip
        with pytest.raises(ValidationFailed):
            ingest.parse_twitch_login(bad)


def test_segments_are_ordered_and_the_open_one_is_excluded(tmp_path):
    session = _session(tmp_path / "s1", [1010, 1020, 1030])
    (session / "notes.txt").write_text("not a segment")
    assert [(s.path.name, s.start, s.end) for s in list_segments(session, closed=False)] == [
        ("000000.ts", 1000, 1010), ("000001.ts", 1010, 1020),
    ]  # fmt: skip
    # once the recorder has stopped, the last file is complete too
    assert [(s.start, s.end) for s in list_segments(session, closed=True)] == [(1000, 1010), (1010, 1020), (1020, 1030)]
    assert list_segments(tmp_path / "missing", closed=True) == []


def test_a_burst_of_segments_keeps_every_file(tmp_path):
    # The network stalls, then twenty seconds of stream arrive at once: two segments are closed in the same second.
    session = _session(tmp_path / "s1", [1010, 1030, 1030, 1040])
    assert [(s.start, s.end) for s in list_segments(session, closed=True)] == [(1000, 1010), (1010, 1030), (1030, 1030), (1030, 1040)]
    segments, _ = window_segments(tmp_path, 1005, 1035, None)
    assert [s.path.name for s in segments] == ["000000.ts", "000001.ts", "000002.ts", "000003.ts"]


def test_window_picks_the_session_that_covers_it(tmp_path):
    _session(tmp_path / "900", [1010, 1020, 1030, 1040, 1050, 1060])
    live = _session(tmp_path / "2000", [2010, 2020, 2030, 2040])  # the last one is still being written
    segments, more = window_segments(tmp_path, 1015, 1045, live)
    assert [s.start for s in segments] == [1010, 1020, 1030, 1040] and more is False
    segments, more = window_segments(tmp_path, 2005, 2045, live)
    assert [s.start for s in segments] == [2000, 2010, 2020] and more is True  # the window runs past what is on disk
    segments, more = window_segments(tmp_path, 2005, 2025, live)
    assert [s.start for s in segments] == [2000, 2010, 2020] and more is False
    assert window_segments(tmp_path, 5000, 5060, None) == ([], False)


def test_prune_keeps_what_is_newer_than_the_cutoff(tmp_path):
    old = _session(tmp_path / "900", [1010, 1020, 1030])
    live = _session(tmp_path / "2000", [2010, 2020, 2030, 2040])
    assert prune(tmp_path, before=2015, live_session=live) == 4  # one byte each: three old segments and the first live one
    assert not old.exists()  # an emptied, finished session is removed
    assert sorted(f.name for f in live.iterdir()) == ["000001.ts", "000002.ts", "000003.ts"]
    assert prune(tmp_path, before=9999, live_session=live) == 2
    assert [f.name for f in live.iterdir()] == ["000003.ts"]  # never the segment being written


def test_concat_copy_joins_real_segments(synthetic_video, tmp_path):
    parts = _real_segments(synthetic_video, tmp_path / "session")
    assert len(parts) >= 2
    joined = ffmpeg.concat_copy(parts, tmp_path / "it's joined.mp4")  # a quote in the path must not break the list file
    probe = ffmpeg.probe(joined)
    assert abs(probe.duration_ms - 24_000) < 500
    assert probe.has_audio and probe.video_codec == "h264"
    ffmpeg.decode_check(joined)
    assert sorted(f.name for f in tmp_path.iterdir()) == ["it's joined.mp4", "session"]  # no temp file left behind


@pytest.mark.slow
def test_recorder_segments_a_stream_and_notices_when_it_ends(tmp_path, monkeypatch):
    monkeypatch.setattr(recorder, "SEGMENT_S", 1)

    class ShortStream(Recorder):
        def _source_command(self) -> list[str]:
            return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-re", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=8",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=8", "-c:v", "libx264", "-preset", "ultrafast", "-g", "15",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-f", "mpegts", "pipe:1"]  # fmt: skip

    rec = ShortStream("marlon", tmp_path)
    deadline = time.time() + 20
    while rec.poll(time.time()) != "recording" and time.time() < deadline:
        time.sleep(0.1)
    assert rec.poll(time.time()) == "recording" and rec.recording_since is not None
    session = rec.session_dir
    while rec.poll(time.time()) == "recording" and time.time() < deadline:
        time.sleep(0.1)
    assert rec.poll(time.time()) == "offline"  # the stream ended; the recorder waits for the next one
    assert len(list_segments(session, closed=True)) >= 3
    rec.stop()
    assert rec.poll(time.time() + 3600) == "offline"  # a stopped recorder never restarts on its own


class ScriptedRecorder(Recorder):
    """The real state machine, with a stand-in for the streamlink/ffmpeg pair: the test writes the segments itself."""

    def __init__(self, folder: Path, starts: list) -> None:
        super().__init__("marlon", folder)
        self.starts = starts  # one entry per start attempt: an exception to raise, or a command to run
        self.attempts = 0

    def _spawn(self) -> list[subprocess.Popen]:
        self.attempts += 1
        step = self.starts.pop(0)
        if isinstance(step, Exception):
            raise step
        return [subprocess.Popen(step)]

    def segment(self, at: float, size: int = 1) -> None:
        index = len(list(self.session_dir.glob("*.ts")))
        path = self.session_dir / f"{index:06d}.ts"
        path.write_bytes(b"x" * size)
        os.utime(path, (at, at))

    def stream_ends(self) -> None:
        for proc in self._procs:
            proc.terminate()
            proc.wait()


RUNNING = ["sleep", "30"]


def test_recorder_retries_a_failed_start_with_backoff(tmp_path):
    rec = ScriptedRecorder(tmp_path, [OSError("too many open files"), OSError("too many open files"), RUNNING])
    assert rec.poll(1000) == "error" and "too many open files" in rec.detail
    assert rec.poll(1005) == "error" and rec.attempts == 1  # not before the backoff
    assert rec.poll(1011) == "error" and rec.attempts == 2
    assert rec.poll(1025) == "error" and rec.attempts == 2  # the backoff doubled
    assert rec.poll(1032) == "error" and rec.attempts == 3  # started at last, but not trusted yet
    assert rec.poll(1063) == "offline"  # it has stayed up for half a minute: it is simply waiting for the stream
    rec.stop()


def test_recorder_that_dies_without_recording_is_an_error(tmp_path):
    rec = ScriptedRecorder(tmp_path, [["true"], RUNNING])
    rec.poll(1000)
    rec.stream_ends()
    assert rec.poll(1001) == "error"  # it exited before any footage: not "the stream ended"
    assert rec.poll(1005) == "error" and rec.attempts == 1
    assert rec.poll(1012) == "error" and rec.attempts == 2  # restarted, but not trusted yet
    assert rec.poll(1045) == "offline"  # it has stayed up for half a minute: it is simply waiting for the stream
    rec.stop()


def test_stream_hiccup_keeps_the_recording_start(tmp_path):
    rec = ScriptedRecorder(tmp_path, [RUNNING, RUNNING, RUNNING])
    assert rec.poll(1000) == "offline"
    rec.segment(1010)
    assert rec.poll(1010) == "recording" and rec.recording_since == 1010
    first_session = rec.session_dir

    rec.stream_ends()  # a hiccup: the stream drops and is back within seconds
    assert rec.poll(1200) == "offline"
    assert rec.poll(1206) == "offline" and rec.attempts == 2 and rec.session_dir != first_session
    rec.segment(1215)
    assert rec.poll(1215) == "recording"
    assert rec.recording_since == 1010  # footage is back 15 s after it stopped: same broadcast, no second warm-up

    rec.stream_ends()  # the broadcast really ends; the next one starts an hour later
    rec.poll(1300)
    assert rec.poll(4900) == "offline" and rec.attempts == 3
    rec.segment(4910)
    assert rec.poll(4910) == "recording" and rec.recording_since == 4910
    rec.stop()
    assert rec.poll(9999) == "offline" and rec.attempts == 3  # a stopped recorder never restarts on its own


def test_recorder_restarts_a_stalled_stream(tmp_path):
    rec = ScriptedRecorder(tmp_path, [RUNNING, RUNNING])
    rec.poll(1000)
    rec.segment(1010)
    assert rec.poll(1500) == "recording"  # eight minutes without a segment can be an ad break
    process = rec._procs[0]
    assert rec.poll(1700) == "offline"  # more than ten: the recorder is hung
    assert process.poll() is not None
    assert rec.poll(1706) == "offline" and rec.attempts == 2
    rec.stop()


def test_an_empty_first_segment_is_not_a_recording(tmp_path):
    rec = ScriptedRecorder(tmp_path, [RUNNING, RUNNING])
    rec.poll(1000)
    rec.segment(1001, size=0)  # the segmenter opened its first file, then everything died
    rec.stream_ends()
    assert rec.poll(1002) == "error"
    rec.stop()


# ── data ────────────────────────────────────────────────────────────────────


def test_epoch_round_trip():
    assert to_epoch(from_epoch(1_794_924_600.5)) == 1_794_924_600.5
    assert from_epoch(0).isoformat() == "1970-01-01T00:00:00"  # naive UTC, like every datetime in the database
    assert abs(to_epoch(now()) - time.time()) < 2


def test_live_settings_have_the_documented_defaults():
    live = RuntimeSettings().live
    assert (live.spike_z, live.min_chatters, live.before_s, live.after_s, live.buffer_min) == (3.5, 5, 45, 15, 10)


# ── watcher ─────────────────────────────────────────────────────────────────

T = 1_794_000_000  # a bucket boundary


class FakeRecorder:
    """Stands in for the streamlink/ffmpeg pair: the test decides what it reports."""

    def __init__(self, login: str, channel_dir: Path) -> None:
        self.login, self.channel_dir = login, channel_dir
        self.state, self.recording_since, self.session_dir, self.detail = "offline", None, None, ""
        self.started = self.stopped = False

    def poll(self, now: float) -> str:
        if not self.started:  # like the real one: the first poll starts a new, empty session
            self.started = True
            self.session_dir = self.channel_dir / "fake-session"
            self.session_dir.mkdir(parents=True, exist_ok=True)
        return self.state

    def stop(self) -> None:
        self.stopped = True


class FakeChat:
    def __init__(self) -> None:
        self.channels: set[str] = set()
        self.up = True

    def set_channels(self, logins: set[str]) -> None:
        self.channels = logins

    def covered(self, channel: str, t0: int) -> bool:
        return self.up


class Live:
    """A watcher on one channel, driven bucket by bucket."""

    def __init__(self, settings) -> None:
        self.settings = settings
        with session_scope() as s:
            channel = LiveChannel(login="marlon", rights_confirmed=True, settings=live_project_settings("gta6").model_dump(mode="json"))
            s.add(channel)
            s.flush()
            self.channel_id = channel.id
        self.folder = settings.path("live", self.channel_id)
        self.chat, self.counter, self.recorders = FakeChat(), ChatCounter(), []
        self.watcher = self.restart()
        self.now = T

    def restart(self) -> Watcher:
        def factory(login: str, channel_dir: Path) -> FakeRecorder:
            self.recorders.append(FakeRecorder(login, channel_dir))
            return self.recorders[-1]

        self.watcher = Watcher(self.settings, chat=self.chat, counter=self.counter, recorder_factory=factory)
        return self.watcher

    @property
    def recorder(self) -> FakeRecorder:
        return self.recorders[-1]

    def bucket(self, chatters: int) -> None:
        """Ten seconds pass, during which ``chatters`` different people write in chat."""
        for i in range(chatters):
            self.counter.add("marlon", f"user{i}", self.now + 1)
        self.now += 10
        self.watcher.tick(self.now)

    def moments(self) -> list[LiveMoment]:
        with session_scope() as s:
            return list(s.scalars(select(LiveMoment).order_by(LiveMoment.created_at)))

    def channel(self) -> LiveChannel:
        with session_scope() as s:
            return s.get(LiveChannel, self.channel_id)

    def pending_moment(self, start: float, end: float) -> str:
        with session_scope() as s:
            moment = LiveMoment(channel_id=self.channel_id, peak_at=from_epoch(start + 45), chatters=60, baseline=8.0, z=18.4,
                                window_start=from_epoch(start), window_end=from_epoch(end))  # fmt: skip
            s.add(moment)
            s.flush()
            return moment.id


@pytest.fixture
def live(settings) -> Live:
    return Live(settings)


def test_spike_while_recording_creates_a_pending_moment(live):
    live.watcher.tick(T)
    assert live.recorder.started and live.chat.channels == {"marlon"}
    live.recorder.state, live.recorder.recording_since = "recording", T - 300
    for _ in range(8):
        live.bucket(8)
    assert live.moments() == []
    live.bucket(60)
    (moment,) = live.moments()
    spike_at = T + 80
    assert (moment.status, moment.chatters, moment.baseline) == ("pending", 60, 8.0)
    assert (to_epoch(moment.peak_at), to_epoch(moment.window_start), to_epoch(moment.window_end)) == (spike_at, spike_at - 45, spike_at + 15)
    live.bucket(70)  # the same moment, still hot
    assert len(live.moments()) == 1

    channel = live.channel()
    assert channel.state == "recording" and to_epoch(channel.live_since) == T - 300
    assert channel.chat == {"t0": T, "chatters": [8] * 8 + [60, 70]}
    with session_scope() as s:
        assert s.get(AppSetting, "live_heartbeat") is not None


def test_no_moment_when_offline_or_warming_up(live):
    live.watcher.tick(T)
    for _ in range(8):
        live.bucket(8)
    live.bucket(60)  # chat explodes while the channel is offline: nothing was recorded, nothing to clip
    for _ in range(3):
        live.bucket(8)
    live.recorder.state, live.recorder.recording_since = "recording", live.now - 30
    live.bucket(60)  # thirty seconds into the stream: everyone is saying hello
    assert live.moments() == []
    assert live.channel().state == "recording"


def test_chat_gap_does_not_fake_a_spike(live):
    live.watcher.tick(T)
    live.recorder.state, live.recorder.recording_since = "recording", T - 300
    for _ in range(8):
        live.bucket(8)
    live.chat.up = False  # the chat connection drops for more than three minutes
    for _ in range(20):
        live.bucket(0)
    live.chat.up = True
    for _ in range(3):
        live.bucket(8)  # back at its usual level: against a baseline of fake zeros this would be a spike
    assert live.moments() == []
    assert live.channel().chat["chatters"][8:28] == [None] * 20  # the outage is shown as unknown, not as silence


def test_pending_moment_survives_a_restart_and_becomes_a_queued_project(live, synthetic_video):
    # Before the restart: 24 s were recorded around the spike, then the stream (and the watcher) stopped.
    _real_segments(synthetic_video, live.folder / "old-session", start=T - 40)
    moment_id = live.pending_moment(T - 45, T + 15)
    live.restart()
    live.watcher.tick(T + 30)
    assert live.moments()[0].status == "pending"  # the new recorder has nothing yet: footage for the window may still arrive

    live.watcher.tick(T + 15 + 46)  # it did not: the moment is made from what was recorded
    moment = live.moments()[0]
    assert moment.id == moment_id and moment.status == "queued", moment.detail
    with session_scope() as s:
        project = s.get(Project, moment.project_id)
        source, job = project.source, project.jobs[0]
        assert (source.kind, source.status, source.channel, source.origin_url) == ("twitch", "ready", "marlon", "https://www.twitch.tv/marlon")
        assert abs(source.duration_ms - 24_000) < 500 and source.rights_confirmed
        assert live.settings.path(source.path).exists()
        assert project.settings["clip_count"] == 1 and project.settings["quality_threshold"] == 0
        assert (job.kind, job.status, job.priority) == ("pipeline", "queued", 10)
    live.watcher.tick(T + 100)
    with session_scope() as s:
        assert len(list(s.scalars(select(Job)))) == 1  # materialised once


def test_moment_without_footage_fails_with_a_reason(live):
    live.pending_moment(T - 45, T + 15)
    live.watcher.tick(T + 15 + 46)
    (moment,) = live.moments()
    assert (moment.status, moment.detail) == ("failed", NO_FOOTAGE)
    with session_scope() as s:
        assert s.scalar(select(Source.id)) is None


def test_two_seconds_of_footage_are_not_a_clip(live, tmp_path):
    # The stream ended two seconds into the window: one short segment is all there is.
    short = tmp_path / "short.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=2",
                    "-f", "lavfi", "-i", "sine=duration=2", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", str(short)], check=True)  # fmt: skip
    _real_segments(short, live.folder / "old-session", start=T - 10)
    live.pending_moment(T - 45, T + 15)
    live.watcher.tick(T + 15 + 46)
    (moment,) = live.moments()
    assert (moment.status, moment.detail) == ("failed", NO_FOOTAGE)
    with session_scope() as s:
        assert s.scalar(select(Source.id)) is None
    assert not list(live.settings.path("sources").iterdir()) and not list(live.settings.path("thumbnails").iterdir())


def test_moment_deleted_before_assembly_leaves_nothing_behind(live, synthetic_video):
    parts = _real_segments(synthetic_video, live.folder / "old-session", start=T - 40)
    with pytest.raises(ClipFactoryError):
        materialize(live.settings, "no-such-moment", parts)
    assert not list(live.settings.path("sources").iterdir())


def test_spike_is_saved_even_when_the_rest_of_the_tick_goes_wrong(live, monkeypatch):
    live.watcher.tick(T)
    live.recorder.state, live.recorder.recording_since = "recording", T - 300
    for _ in range(8):
        live.bucket(8)
    _session(live.folder / "old-session", [T - 960, T - 950, T - 940])
    older = live.pending_moment(T - 1000, T - 900)

    def broken(*_args) -> None:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(watcher_module, "materialize", broken)
    live.bucket(60)  # chat spikes in the very tick where assembling the older moment blows up
    by_id = {m.id: m for m in live.moments()}
    assert by_id.pop(older).status == "failed"  # with a reason, and without taking the tick down
    (spike,) = by_id.values()
    assert (spike.status, spike.chatters) == ("pending", 60)
    with session_scope() as s:
        assert s.get(AppSetting, HEARTBEAT_KEY) is not None


def test_one_broken_recorder_does_not_stop_the_others(live):
    with session_scope() as s:
        s.add(LiveChannel(login="pote", rights_confirmed=True, settings=live_project_settings("gta6").model_dump(mode="json")))
    live.watcher.tick(T)
    marlon, pote = sorted(live.recorders, key=lambda r: r.login)
    marlon.poll = lambda now: (_ for _ in ()).throw(OSError("disk error"))
    pote.state, pote.recording_since = "recording", T - 300
    live.watcher.tick(T + 1)
    with session_scope() as s:
        states = {c.login: c.state for c in s.scalars(select(LiveChannel))}
    assert states == {"marlon": "error", "pote": "recording"}


def test_unreadable_footage_fails_instead_of_crashing(live):
    _session(live.folder / "old-session", [T - 20, T - 10, T])  # three files that are not video
    live.pending_moment(T - 45, T + 15)
    live.watcher.tick(T + 15 + 46)
    (moment,) = live.moments()
    assert moment.status == "failed" and moment.detail
    with session_scope() as s:
        assert s.scalar(select(Source.id)) is None
    assert not list(live.settings.path("sources").iterdir())


def test_buffer_is_pruned_but_never_under_a_pending_moment(live):
    live.watcher.tick(T)
    session = _session(live.folder / "session", [T - 3000, T - 2000, T - 900, T - 890, T - 30, T - 20])
    live.recorder.session_dir, live.recorder.state = session, "recording"
    live.pending_moment(T - 1000, T + 60)  # still waiting for its footage, and it reaches back past the 10-minute buffer
    live.bucket(0)
    assert sorted(f.name for f in session.iterdir()) == ["000002.ts", "000003.ts", "000004.ts", "000005.ts"]


def test_removed_channel_stops_its_recorder(live):
    live.watcher.tick(T)
    _session(live.folder / "session", [T - 20, T - 10])
    with session_scope() as s:
        s.get(LiveChannel, live.channel_id).enabled = False
    live.watcher.tick(T + 1)
    assert live.recorder.stopped and live.chat.channels == set()
    assert not live.folder.exists()


# ── pipeline ────────────────────────────────────────────────────────────────


def test_credit_is_appended_and_channel_becomes_a_hashtag():
    social = {"title": "Il a pas fait ça", "caption": "Marlon tente le saut.", "hashtags": ["#gta6"], "origin": "ai"}
    live = credit_social(social, "marlon", "https://www.twitch.tv/marlon")
    assert live == {**social, "caption": "Marlon tente le saut.\n🎥 twitch.tv/marlon", "hashtags": ["#gta6", "#marlon"]}
    assert social["hashtags"] == ["#gta6"]  # the input is left alone

    already = credit_social({**social, "hashtags": ["#gta6", "#Marlon"]}, "marlon", "https://www.twitch.tv/marlon")
    assert already["hashtags"] == ["#gta6", "#Marlon"]  # tagged by the model already: not twice

    full = {"title": "t", "caption": "", "hashtags": [f"#tag{i}" for i in range(10)], "origin": "default"}
    imported = credit_social(full, "Marlon TV", "https://www.youtube.com/watch?v=abcdefghijk")
    assert imported["caption"] == "🎥 Marlon TV"  # an imported video is credited by its channel name
    assert len(imported["hashtags"]) == 10 and imported["hashtags"][-1] == "#marlontv"


def test_no_credit_without_a_channel():
    social = {"title": "t", "caption": "c", "hashtags": ["#gta6"], "origin": "ai"}
    assert credit_social(social, None, None) == social


def _live_job(monkeypatch, synthetic_video, **project_settings) -> None:
    """A recorded chat moment, taken through the worker's pipeline with the test adapters."""
    monkeypatch.setenv("CLIPFACTORY_AI_ADAPTER", "fake")
    monkeypatch.setenv("CLIPFACTORY_TRANSCRIBER", "fake")
    config.get_settings.cache_clear()
    db.reset_engine()
    live = Live(config.get_settings())
    with session_scope() as s:
        channel = s.get(LiveChannel, live.channel_id)
        channel.settings = {**channel.settings, **project_settings}
    _real_segments(synthetic_video, live.folder / "old-session", start=T - 40)
    live.pending_moment(T - 45, T + 15)
    live.watcher.tick(T + 15 + 46)
    assert run_job(live.settings, claim_next_job("test-worker"), threading.Event()) == "succeeded"


OBJECTION = "Rien de marquant ne se passe."


def _final_check_rejects(monkeypatch) -> list:
    """The final check rejects every clip. Returns the list that fills with the briefs it is shown."""
    shown = []

    def critique(self, briefs, ctx):  # noqa: ANN001, ANN202
        shown.extend(briefs)
        return {b.candidate_id: Critique(candidate_id=b.candidate_id, verdict="reject", issue=OBJECTION, start_offset_s=0, end_offset_s=0)
                for b in briefs}  # fmt: skip

    monkeypatch.setattr(FakeAnalyst, "critique", critique)
    return shown


@pytest.mark.slow
def test_live_moment_keeps_its_clip_when_the_final_check_objects(settings, monkeypatch, synthetic_video):
    _final_check_rejects(monkeypatch)
    _live_job(monkeypatch, synthetic_video)
    with session_scope() as s:
        (clip,) = s.scalars(select(Clip))  # no quality threshold: the person posting decides, with the objection in view
        assert clip.render_status == "rendered"
        chosen = s.get(Candidate, clip.candidate_id)
        assert chosen.status == "selected" and f"réserve du contrôle qualité : {OBJECTION}" in chosen.evidence["refinement"]
        assert s.scalar(select(Project)).summary["selected"] == 1


@pytest.mark.slow
def test_final_check_still_rejects_when_a_quality_threshold_is_set(settings, monkeypatch, synthetic_video):
    _final_check_rejects(monkeypatch)
    _live_job(monkeypatch, synthetic_video, quality_threshold=1)
    with session_scope() as s:
        assert s.scalars(select(Clip)).all() == []
        assert OBJECTION in [c.reject_reason for c in s.scalars(select(Candidate))]


@pytest.mark.slow
def test_final_check_is_not_shown_a_title_that_does_not_exist(settings, monkeypatch, synthetic_video):
    monkeypatch.setattr(FakeAnalyst, "find_moments", lambda self, chunks, per_chunk, ctx: [])  # signals only: no title yet
    shown = _final_check_rejects(monkeypatch)
    _live_job(monkeypatch, synthetic_video, quality_threshold=1)
    assert shown and all(value for b in shown for value in (b.moment or {}).values())


@pytest.mark.slow
def test_live_moment_goes_through_the_pipeline_with_a_credited_post(settings, monkeypatch, synthetic_video):
    _live_job(monkeypatch, synthetic_video)
    with session_scope() as s:
        assert any("chat_spike" in c.origins for c in s.scalars(select(Candidate)))  # the moment chat pointed at competes
        assert any("60 personnes" in line.message for line in s.scalars(select(JobLog)))
        (clip,) = s.scalars(select(Clip))
        assert clip.render_status == "rendered" and clip.validation["ok"] and clip.is_test
        assert clip.social["caption"].endswith("\n🎥 twitch.tv/marlon")
        assert clip.social["hashtags"] == ["#test", "#marlon"]


# ── API ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def client(settings):
    from clipfactory.api.app import app

    with TestClient(app) as test_client:
        yield test_client


def _add(client, channel: str = "marlon", **extra) -> dict:
    response = client.post("/api/live/channels", json={"channel": channel, "rights_confirmed": True, **extra})
    assert response.status_code == 200, response.text
    return response.json()["channel"]


def test_channel_needs_rights_and_a_valid_login(client):
    assert client.post("/api/live/channels", json={"channel": "marlon"}).status_code == 422  # authorisation not confirmed
    assert client.post("/api/live/channels", json={"channel": "https://evil.example/marlon", "rights_confirmed": True}).status_code == 422
    channel = _add(client, "https://www.twitch.tv/Marlon", profile="gta_rp")
    assert (channel["login"], channel["url"], channel["enabled"], channel["profile"], channel["state"]) == (
        "marlon", "https://www.twitch.tv/marlon", True, "gta_rp", "offline",
    )  # fmt: skip
    with session_scope() as s:
        row = s.scalar(select(LiveChannel))
        assert row.rights_confirmed and row.settings == live_project_settings("gta_rp").model_dump(mode="json")


def test_channel_cannot_be_added_twice(client):
    _add(client)
    assert client.post("/api/live/channels", json={"channel": "@Marlon", "rights_confirmed": True}).status_code == 409


def test_live_overview_lists_channels_and_moments(client):
    channel = _add(client)
    overview = client.get("/api/live").json()
    assert overview["alive"] is False and overview["moments"] == [] and [c["login"] for c in overview["channels"]] == ["marlon"]
    assert client.get("/api/health").json()["live_alive"] is False

    with session_scope() as s:  # what a running watcher writes
        row = s.get(LiveChannel, channel["id"])
        row.state, row.live_since, row.chat = "recording", from_epoch(T - 300), {"t0": T, "chatters": [8, None, 60]}
        s.add(AppSetting(key=HEARTBEAT_KEY, value={"n": 1}))
        s.add(LiveMoment(channel_id=row.id, peak_at=from_epoch(T), chatters=60, baseline=8.0, z=18.4, window_start=from_epoch(T - 45),
                         window_end=from_epoch(T + 15), status="failed", detail="Aucune image"))  # fmt: skip
        s.add(LiveMoment(channel_id=row.id, peak_at=from_epoch(T + 300), chatters=45, baseline=9.0, z=12.0,
                         window_start=from_epoch(T + 255), window_end=from_epoch(T + 315)))  # fmt: skip
    overview = client.get("/api/live").json()
    assert overview["alive"] is True
    (seen,) = overview["channels"]
    assert seen["state"] == "recording" and seen["live_since"].endswith("Z") and seen["chat"] == {"t0": T, "chatters": [8, None, 60]}
    newest, oldest = overview["moments"]
    assert (newest["login"], newest["chatters"], newest["baseline"], newest["status"], newest["clip"]) == ("marlon", 45, 9.0, "pending", None)
    assert (oldest["status"], oldest["detail"]) == ("failed", "Aucune image")
    health = client.get("/api/health").json()
    assert health["live_alive"] is True and health["dependencies"]["streamlink"]

    with session_scope() as s:  # the watcher died a minute ago: whatever it last wrote, nothing is being recorded
        s.execute(update(AppSetting).where(AppSetting.key == HEARTBEAT_KEY).values(updated_at=from_epoch(time.time() - 60)))
    stale = client.get("/api/live").json()
    assert stale["alive"] is False and stale["channels"][0]["state"] == "offline" and stale["channels"][0]["chat"] is None


def test_moment_shows_its_clip_once_the_pipeline_made_one(client):
    channel = _add(client)
    with session_scope() as s:
        source = Source(kind="twitch", status="ready", title="marlon — direct", channel="marlon", duration_ms=60_000)
        s.add(source)
        s.flush()
        project = Project(source_id=source.id, name=source.title, settings=channel_settings(s, channel["id"]))
        s.add(project)
        s.flush()
        s.add(Job(project_id=project.id, kind="pipeline", status="succeeded"))
        s.add(Clip(project_id=project.id, rank=1, start_ms=5_000, end_ms=40_000, original_start_ms=5_000, original_end_ms=40_000,
                   score=61, title="Il a pas fait ça", render_status="rendered"))  # fmt: skip
        s.add(LiveMoment(channel_id=channel["id"], peak_at=from_epoch(T), chatters=60, baseline=8.0, z=18.4,
                         window_start=from_epoch(T - 45), window_end=from_epoch(T + 15), status="queued", project_id=project.id))  # fmt: skip
    (moment,) = client.get("/api/live").json()["moments"]
    assert (moment["status"], moment["job_status"]) == ("queued", "succeeded")
    assert moment["clip"]["title"] == "Il a pas fait ça" and moment["clip"]["render_status"] == "rendered"


def channel_settings(s, channel_id: str) -> dict:
    return s.get(LiveChannel, channel_id).settings


def test_channel_can_be_paused_and_removed(client):
    channel = _add(client)
    with session_scope() as s:
        s.get(LiveChannel, channel["id"]).state = "recording"
        s.add(AppSetting(key=HEARTBEAT_KEY, value={"n": 1}))
        s.add(LiveMoment(channel_id=channel["id"], peak_at=from_epoch(T), chatters=60, baseline=8.0, z=18.4,
                         window_start=from_epoch(T - 45), window_end=from_epoch(T + 15)))  # fmt: skip
    paused = client.patch(f"/api/live/channels/{channel['id']}", json={"enabled": False, "profile": "general"}).json()["channel"]
    assert (paused["enabled"], paused["profile"], paused["state"]) == (False, "general", "offline")  # a paused channel is not recorded
    with session_scope() as s:
        assert s.get(LiveChannel, channel["id"]).settings["clip_count"] == 1  # only the profile changed

    assert client.delete(f"/api/live/channels/{channel['id']}").status_code == 200
    overview = client.get("/api/live").json()
    assert overview["channels"] == [] and overview["moments"] == []
    assert client.delete(f"/api/live/channels/{channel['id']}").status_code == 404
    assert client.patch(f"/api/live/channels/{channel['id']}", json={"enabled": True}).status_code == 404


# ── processes ───────────────────────────────────────────────────────────────


def test_processes_starting_together_migrate_once(tmp_path):
    # scripts/dev.sh starts the API, the worker and the watcher at the same instant, and each one migrates.
    env = {**os.environ, "CLIPFACTORY_DATA_DIR": str(tmp_path / "data")}
    starts = [subprocess.Popen([sys.executable, "-c", "from clipfactory.db import migrate; migrate()"], env=env, stderr=subprocess.PIPE)
              for _ in range(6)]  # fmt: skip
    failures = [p.stderr.read().decode()[-300:] for p in starts if p.wait(timeout=60) != 0]
    assert failures == []


def test_only_one_watcher_per_data_folder(settings):
    first = single_instance(settings)
    with pytest.raises(Conflict):  # a second scripts/dev.sh: it must not stop the first one's recorders
        single_instance(settings)
    first.close()
    single_instance(settings).close()


@pytest.mark.skipif(shutil.which("caffeinate") is None, reason="macOS only")
def test_the_mac_is_kept_awake_while_a_channel_is_watched():
    assert keep_awake(None, False) is None
    holder = keep_awake(None, True)
    try:
        assert holder.poll() is None and "caffeinate" in holder.args[0]
        assert keep_awake(holder, True) is holder
        assert keep_awake(holder, False) is None
        assert holder.wait(timeout=5) is not None  # released as soon as nothing is watched
    finally:
        holder.kill()
