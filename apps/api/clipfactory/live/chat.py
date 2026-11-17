"""Read-only Twitch chat: who is talking in which channel, counted in 10-second buckets.

Anonymous IRC over TLS (a ``justinfan`` nick, no credentials, nothing is ever sent to a channel).
The connection is expected to drop: the client reconnects and re-joins on its own, and ``covered``
tells the caller which buckets were actually listened to, so an outage is never read as a silent chat.
A message is counted in the bucket of the moment Twitch sent it, not of the moment it reached us: after
a stalled link, the backlog falls into buckets that are already closed instead of faking a spike.
"""

from __future__ import annotations

import random
import socket
import ssl
import threading
import time

from clipfactory.analysis.chat import BUCKET_S
from clipfactory.logs import get_logger

log = get_logger("chat")
_PING_EVERY_S = 10
_SILENCE_LIMIT_S = 30  # every ping is answered, so this long without a byte means the link is dead
_HELD_S = 60  # a connection that lasted this long was a good one: the next outage starts the backoff over


def parse_privmsg(line: str) -> tuple[str, str, float | None] | None:
    """(channel login, user login, when Twitch sent it) of a chat message line, or None for anything else.

    The send time (epoch seconds) comes from the ``tmi-sent-ts`` tag and is None when the line has none.
    """
    sent = None
    if line.startswith("@"):  # IRCv3 tags
        tags, _, line = line.partition(" ")
        stamp = next((tag[12:] for tag in tags[1:].split(";") if tag.startswith("tmi-sent-ts=")), "")
        sent = int(stamp) / 1000 if stamp.isdigit() else None
    if not line.startswith(":"):
        return None
    prefix, _, rest = line[1:].partition(" ")
    command, _, rest = rest.partition(" ")
    target = rest.partition(" ")[0]
    user = prefix.partition("!")[0]
    if command != "PRIVMSG" or not target.startswith("#") or not user:
        return None
    return target[1:].lower(), user.lower(), sent


class ChatCounter:
    """Thread-safe tally of chatters per channel and bucket. Written by the chat thread, read by the watcher."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buckets: dict[str, dict[int, set[str]]] = {}

    def add(self, channel: str, user: str, at: float) -> None:
        t0 = int(at) // BUCKET_S * BUCKET_S
        with self._lock:
            self._buckets.setdefault(channel, {}).setdefault(t0, set()).add(user)

    def take(self, channel: str, t0: int) -> int:
        """Distinct chatters of the bucket starting at ``t0``. It and anything older is forgotten, so a
        message that turns up for a bucket already read is dropped rather than counted somewhere else."""
        with self._lock:
            buckets = self._buckets.get(channel, {})
            chatters = len(buckets.get(t0, ()))
            for key in [k for k in buckets if k <= t0]:
                del buckets[key]
        return chatters


class ChatClient(threading.Thread):
    def __init__(self, counter: ChatCounter, stop: threading.Event, *, host: str = "irc.chat.twitch.tv", port: int = 6697,
                 tls: bool = True) -> None:  # fmt: skip
        super().__init__(daemon=True, name="twitch-chat")
        self._counter = counter
        self._stop_event = stop
        self._host, self._port, self._tls = host, port, tls
        self._lock = threading.Lock()
        self._wanted: set[str] = set()
        self._joined: dict[str, float] = {}  # channel → when we started listening on the current connection

    def set_channels(self, logins: set[str]) -> None:
        with self._lock:
            self._wanted = set(logins)

    def covered(self, channel: str, t0: int) -> bool:
        """True when the whole bucket starting at ``t0`` was listened to on the current connection."""
        with self._lock:
            since = self._joined.get(channel)
        return since is not None and since <= t0

    def run(self) -> None:
        delay = 1.0
        while not self._stop_event.is_set():
            started = time.monotonic()
            try:
                self._session()
            except Exception as exc:  # noqa: BLE001 - this thread must outlive anything a connection throws at it
                log.warning("chat connection lost: %s", exc)
            with self._lock:
                self._joined.clear()
            if time.monotonic() - started >= _HELD_S:
                delay = 1.0
            if self._stop_event.wait(delay):
                return
            delay = min(30.0, delay * 2)

    def _session(self) -> None:
        """One connection, until it breaks (by raising) or we are told to stop."""
        raw = socket.create_connection((self._host, self._port), timeout=10)
        sock = ssl.create_default_context().wrap_socket(raw, server_hostname=self._host) if self._tls else raw
        with sock:
            sock.settimeout(1.0)

            def send(line: str) -> None:
                sock.sendall(f"{line}\r\n".encode())

            # tags: when each message was sent. commands: Twitch announces RECONNECT before it restarts a server.
            send("CAP REQ :twitch.tv/tags twitch.tv/commands")
            send(f"NICK justinfan{random.randint(10_000, 99_999)}")
            buf = b""
            last_data = last_ping = time.monotonic()
            while not self._stop_event.is_set():
                self._sync(send)
                now = time.monotonic()
                if now - last_ping >= _PING_EVERY_S:
                    send("PING :clipfactory")
                    last_ping = now
                if now - last_data > _SILENCE_LIMIT_S:
                    raise TimeoutError("no data from Twitch chat")
                try:
                    data = sock.recv(65536)
                except TimeoutError:
                    continue
                if not data:
                    raise ConnectionError("Twitch closed the chat connection")
                last_data = time.monotonic()
                *lines, buf = (buf + data).split(b"\r\n")
                for raw_line in lines:
                    line = raw_line.decode("utf-8", "replace")
                    if line.startswith("PING"):
                        send("PONG" + line[4:])
                    elif line.startswith(":tmi.twitch.tv RECONNECT"):
                        raise ConnectionError("Twitch asked us to reconnect")
                    elif parsed := parse_privmsg(line):
                        channel, user, sent = parsed
                        self._counter.add(channel, user, sent or time.time())

    def _sync(self, send) -> None:  # noqa: ANN001
        """Join and part so that the connection listens to exactly the wanted channels."""
        with self._lock:
            join = sorted(self._wanted - self._joined.keys())
            part = sorted(self._joined.keys() - self._wanted)
            for channel in join:
                self._joined[channel] = time.time()
            for channel in part:
                del self._joined[channel]
        for channel in join:
            send(f"JOIN #{channel}")
        for channel in part:
            send(f"PART #{channel}")
