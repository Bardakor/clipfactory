"""Source ingestion: local uploads and authorised YouTube imports."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from clipfactory.config import Settings
from clipfactory.errors import IngestError, MediaError, ValidationFailed
from clipfactory.logs import get_logger
from clipfactory.media import ffmpeg
from clipfactory.media.proc import ShouldStop, run
from clipfactory.models import MediaAsset, Source

log = get_logger("ingest")

ALLOWED_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_THUMB_HOSTS = (".ytimg.com", ".ggpht.com", ".googleusercontent.com")
MAX_YOUTUBE_DURATION_S = 12 * 3600


class YouTubeInfo(BaseModel):
    video_id: str
    url: str
    title: str
    channel: str | None
    duration_s: int | None
    thumbnail_url: str | None
    is_live: bool


def safe_title(name: str) -> str:
    """Display-only name: never used to build a filesystem path."""
    stem = Path(name.replace("\\", "/")).name
    stem = re.sub(r"[\x00-\x1f\x7f]", "", stem).strip().strip(".")
    return stem[:200] or "video"


def validated_extension(filename: str) -> str:
    ext = Path(filename.replace("\\", "/")).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ValidationFailed(
            f"unsupported file type '{ext or '?'}'", allowed=sorted(ALLOWED_EXTENSIONS)
        )
    return ext


def fingerprint(path: Path) -> str:
    """Content fingerprint: size plus three 4 MB samples (fast even on multi-GB files)."""
    size = path.stat().st_size
    h = hashlib.sha256(str(size).encode())
    chunk = 4 * 1024 * 1024
    with path.open("rb") as f:
        for offset in (0, max(0, size // 2 - chunk // 2), max(0, size - chunk)):
            f.seek(offset)
            h.update(f.read(chunk))
    return h.hexdigest()


def parse_youtube_url(url: str) -> str:
    """Return the 11-character video id, or raise.

    Only the id is kept: every later request goes to a canonical URL we build
    ourselves, so user-supplied hosts, redirects and internal addresses can
    never be reached through this importer.
    """
    try:
        parsed = urlparse(url.strip())
    except ValueError as exc:
        raise ValidationFailed("invalid URL") from exc
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host not in _YOUTUBE_HOSTS or parsed.username or parsed.port:
        raise ValidationFailed("only https://youtube.com and https://youtu.be video links are supported")
    if host == "youtu.be":
        video_id = parsed.path.strip("/").split("/")[0]
    elif parsed.path == "/watch":
        video_id = (parse_qs(parsed.query).get("v") or [""])[0]
    else:
        match = re.match(r"^/(shorts|live|embed)/([^/?]+)", parsed.path)
        video_id = match.group(2) if match else ""
    if not _VIDEO_ID.match(video_id):
        raise ValidationFailed("this does not look like a single YouTube video link")
    return video_id


def canonical_youtube_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def _ytdlp(args: list[str], **kw) -> str:  # noqa: ANN003
    # No cookies, no credentials, no geo/DRM workarounds: only publicly served formats.
    return run([sys.executable, "-m", "yt_dlp", "--no-playlist", "--no-warnings", "--ignore-config", *args], **kw)


def _ytdlp_message(exc: MediaError) -> str:
    stderr = str(exc.details.get("stderr") or "")
    lines = [ln for ln in stderr.splitlines() if "ERROR" in ln]
    return (lines[-1] if lines else stderr[-300:]).replace("ERROR: ", "").strip() or "yt-dlp failed"


def inspect_youtube(url: str) -> YouTubeInfo:
    video_id = parse_youtube_url(url)
    canonical = canonical_youtube_url(video_id)
    try:
        raw = _ytdlp(["--dump-single-json", "--skip-download", canonical], timeout=90)
    except MediaError as exc:
        raise IngestError(f"YouTube refused this video: {_ytdlp_message(exc)}") from exc
    info = json.loads(raw)
    thumb = info.get("thumbnail")
    if thumb:
        t = urlparse(thumb)
        if t.scheme != "https" or not (t.hostname or "").endswith(_THUMB_HOSTS):
            thumb = None
    return YouTubeInfo(
        video_id=video_id,
        url=canonical,
        title=str(info.get("title") or video_id)[:300],
        channel=info.get("channel") or info.get("uploader"),
        duration_s=int(info["duration"]) if info.get("duration") else None,
        thumbnail_url=thumb,
        is_live=bool(info.get("is_live")),
    )


def download_youtube(
    video_id: str,
    dst_dir: Path,
    stem: str,
    *,
    max_bytes: int,
    on_progress: Callable[[float], None] | None = None,
    should_stop: ShouldStop | None = None,
) -> Path:
    def parse(line: str) -> None:
        if on_progress and line.startswith("cf:"):
            parts = line[3:].split("/")
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit() and int(parts[1]) > 0:
                on_progress(min(1.0, int(parts[0]) / int(parts[1])))

    try:
        _ytdlp(
            ["-f", "bv*[height<=1080][vcodec^=avc1]+ba[ext=m4a]/bv*[height<=1080]+ba/b[height<=1080]/b",
             "--merge-output-format", "mp4", "--max-filesize", str(max_bytes), "--no-part", "--newline",
             "--progress-template", "download:cf:%(progress.downloaded_bytes)s/%(progress.total_bytes)s",
             "-o", str(dst_dir / f"{stem}.%(ext)s"), canonical_youtube_url(video_id)],  # fmt: skip
            timeout=4 * 3600,
            on_line=parse,
            should_stop=should_stop,
        )
    except MediaError as exc:
        raise IngestError(f"download failed: {_ytdlp_message(exc)}") from exc
    matches = sorted(dst_dir.glob(f"{stem}.*"))
    if not matches:
        raise IngestError("yt-dlp finished without producing a file (the video may exceed the size limit)")
    return matches[0]


def finalize_source(session: Session, settings: Settings, source: Source, path: Path) -> Source:
    """Probe a freshly stored file, record its metadata and deduplicate by fingerprint.

    Returns the source to use from now on — an existing one if the same content
    was imported before, in which case the new file is removed.
    """
    probe = ffmpeg.probe(path)  # raises MediaError for undecodable input
    fp = fingerprint(path)
    existing = session.scalar(
        select(Source).where(Source.fingerprint == fp, Source.status == "ready", Source.id != source.id)
    )
    if existing is not None and existing.path and settings.path(existing.path).exists():
        log.info("duplicate source", extra={"data": {"existing": existing.id}})
        path.unlink(missing_ok=True)
        session.delete(source)
        return existing
    source.path = settings.rel(path)
    source.fingerprint = fp
    source.bytes = path.stat().st_size
    source.duration_ms = probe.duration_ms
    source.fps = probe.fps
    source.width = probe.width
    source.height = probe.height
    source.video_codec = probe.video_codec
    source.audio_codec = probe.audio_codec
    source.audio_channels = probe.audio_channels
    source.probe = probe.model_dump()
    source.status = "ready"
    source.error = None
    thumb = settings.path("thumbnails", f"{source.id}.jpg")
    try:
        ffmpeg.extract_frame(path, min(probe.duration_ms // 3, 60_000), thumb, width=640)
        session.add(MediaAsset(source_id=source.id, kind="thumbnail", path=settings.rel(thumb),
                               mime="image/jpeg", bytes=thumb.stat().st_size))  # fmt: skip
    except MediaError:
        log.warning("thumbnail extraction failed", extra={"data": {"source": source.id}})
    return source


def delete_source_files(settings: Settings, source_id: str, source_path: str | None, asset_paths: list[str]) -> int:
    """Remove a source and everything derived from it. Returns bytes freed."""
    freed = 0
    targets = [settings.path(p) for p in [source_path, *asset_paths] if p]
    for sub in ("audio", "transcripts", "events", "work", "cache/frames"):
        targets.append(settings.path(sub, source_id))
    targets += list(settings.path("audio").glob(f"{source_id}.*"))
    for target in targets:
        if target.is_dir():
            freed += sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
            shutil.rmtree(target, ignore_errors=True)
        elif target.exists():
            freed += target.stat().st_size
            target.unlink()
    return freed
