"""FFmpeg / ffprobe operations. Every function writes to a temp file and renames on success."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Iterator
from fractions import Fraction
from pathlib import Path

import numpy as np

from clipfactory.domain import ProbeResult
from clipfactory.errors import MediaError
from clipfactory.media.proc import ShouldStop, require, run
from clipfactory.timeutil import ffmpeg_ts, s_to_ms

Progress = Callable[[float], None]
_BASE = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error"]


def _fps(stream: dict) -> float:
    for key in ("avg_frame_rate", "r_frame_rate"):
        raw = stream.get(key) or "0/0"
        try:
            value = float(Fraction(raw))
        except (ValueError, ZeroDivisionError):
            continue
        if 1 <= value <= 240:
            return value
    return 30.0


def probe(path: Path) -> ProbeResult:
    try:
        raw = run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
            timeout=60,
        )
    except MediaError as exc:
        raise MediaError("ffprobe could not read this file", stderr=exc.details.get("stderr")) from exc
    data = json.loads(raw)
    streams = data.get("streams", [])
    video = next(
        (s for s in streams if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")),
        None,
    )
    if video is None:
        raise MediaError("the file contains no video stream")
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fmt = data.get("format", {})
    duration = float(fmt.get("duration") or video.get("duration") or 0)
    if duration <= 0:
        raise MediaError("the file has no measurable duration")
    width, height = int(video["width"]), int(video["height"])
    rotation = abs(int(float(next(
        (sd.get("rotation", 0) for sd in video.get("side_data_list", []) if "rotation" in sd), 0
    ))))  # fmt: skip
    if rotation in (90, 270):
        width, height = height, width
    return ProbeResult(
        duration_ms=s_to_ms(duration),
        fps=round(_fps(video), 3),
        width=width,
        height=height,
        video_codec=video.get("codec_name", "unknown"),
        audio_codec=audio.get("codec_name") if audio else None,
        audio_channels=int(audio["channels"]) if audio and audio.get("channels") else None,
        audio_sample_rate=int(audio["sample_rate"]) if audio and audio.get("sample_rate") else None,
        bit_rate=int(fmt["bit_rate"]) if fmt.get("bit_rate") else None,
        tracks=[
            {
                "index": s.get("index"),
                "type": s.get("codec_type"),
                "codec": s.get("codec_name"),
                "language": (s.get("tags") or {}).get("language"),
                "title": (s.get("tags") or {}).get("title"),
                "channels": s.get("channels"),
                "width": s.get("width"),
                "height": s.get("height"),
            }
            for s in streams
        ],
    )


def _progress_parser(total_ms: int, on_progress: Progress | None) -> Callable[[str], None] | None:
    if on_progress is None or total_ms <= 0:
        return None

    def parse(line: str) -> None:
        if line.startswith("out_time_us="):
            value = line.split("=", 1)[1]
            if value.isdigit():
                on_progress(min(1.0, int(value) / 1000 / total_ms))

    return parse


def _atomic(dst: Path) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    return dst.with_name(f".{dst.stem}.partial{dst.suffix}")


def extract_audio(
    src: Path, dst: Path, *, total_ms: int, on_progress: Progress | None = None, should_stop: ShouldStop | None = None
) -> Path:
    """16 kHz mono PCM — the analysis representation shared by Whisper and the signal detectors."""
    tmp = _atomic(dst)
    run(
        [*_BASE, "-progress", "pipe:1", "-i", str(src), "-vn", "-map", "0:a:0", "-ac", "1", "-ar", "16000",
         "-c:a", "pcm_s16le", str(tmp)],  # fmt: skip
        timeout=4 * 3600,
        on_line=_progress_parser(total_ms, on_progress) or (lambda _l: None),
        should_stop=should_stop,
    )
    tmp.replace(dst)
    return dst


def encode_audio_chunk(src_wav: Path, dst: Path, start_ms: int, end_ms: int) -> Path:
    """Compressed mono MP3 slice for remote speech-to-text uploads."""
    tmp = _atomic(dst)
    run([*_BASE, "-ss", ffmpeg_ts(start_ms), "-t", ffmpeg_ts(end_ms - start_ms), "-i", str(src_wav),
         "-ac", "1", "-c:a", "libmp3lame", "-b:a", "64k", str(tmp)], timeout=300)  # fmt: skip
    tmp.replace(dst)
    return dst


def scan_gray_frames(
    src: Path, *, fps: float = 2.0, size: tuple[int, int] = (160, 90), should_stop: ShouldStop | None = None
) -> Iterator[np.ndarray]:
    """Stream tiny grayscale frames for the cheap first-pass visual scan.

    Frames are yielded one at a time straight from the FFmpeg pipe, so memory use
    is constant regardless of source duration.
    """
    require("ffmpeg")
    w, h = size
    proc = subprocess.Popen(
        [*_BASE, "-i", str(src), "-an", "-vf", f"fps={fps},scale={w}:{h}:flags=fast_bilinear,format=gray",
         "-f", "rawvideo", "pipe:1"],  # fmt: skip
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert proc.stdout
    frame_bytes = w * h
    try:
        while True:
            if should_stop:
                should_stop()
            buf = proc.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                break
            yield np.frombuffer(buf, dtype=np.uint8).reshape(h, w)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def extract_frame(src: Path, at_ms: int, dst: Path, *, width: int = 768, quality: int = 4) -> Path:
    tmp = _atomic(dst)
    run([*_BASE, "-ss", ffmpeg_ts(max(0, at_ms)), "-i", str(src), "-frames:v", "1", "-vf", f"scale={width}:-2",
         "-q:v", str(quality), "-update", "1", str(tmp)], timeout=60)  # fmt: skip
    if not tmp.exists() or tmp.stat().st_size == 0:
        raise MediaError("could not extract a frame", at_ms=at_ms)
    tmp.replace(dst)
    return dst


def cut_clip(
    src: Path,
    dst: Path,
    *,
    start_ms: int,
    end_ms: int,
    fps: int,
    has_audio: bool,
    max_height: int = 1080,
    on_progress: Progress | None = None,
    should_stop: ShouldStop | None = None,
) -> Path:
    """Frame-accurate, re-encoded cut at a constant frame rate.

    ``-ss`` before ``-i`` with re-encoding decodes from the previous keyframe and
    discards frames up to the exact timestamp, so audio and video stay in sync
    with the source. Stream copy would only cut on keyframes and is never used.
    Loudness is normalised conservatively (EBU R128, -14 LUFS, -1.5 dBTP ceiling).
    """
    duration_ms = end_ms - start_ms
    tmp = _atomic(dst)
    cmd = [*_BASE, "-progress", "pipe:1", "-ss", ffmpeg_ts(start_ms), "-i", str(src), "-t", ffmpeg_ts(duration_ms),
           "-map", "0:v:0", "-vf", f"fps={fps},scale=-2:'min({max_height},ih)':flags=lanczos,setsar=1,format=yuv420p",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "15", "-g", str(fps), "-fps_mode", "cfr"]  # fmt: skip
    if has_audio:
        cmd += ["-map", "0:a:0", "-af", "loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000", "-ac", "2",
                "-c:a", "aac", "-b:a", "192k"]  # fmt: skip
    else:
        cmd += ["-an"]
    cmd += ["-movflags", "+faststart", str(tmp)]
    run(cmd, timeout=1800, on_line=_progress_parser(duration_ms, on_progress) or (lambda _l: None), should_stop=should_stop)
    tmp.replace(dst)
    return dst


def segment_command(folder: Path, seconds: int) -> list[str]:
    """Split a stream arriving on stdin into numbered MPEG-TS files of about ``seconds`` each.

    Used by the live recorder, which owns the process. Nothing is re-encoded; segments start on keyframes.
    Files are numbered, not named by time: a burst after a network stall closes several segments in the
    same second, and time-based names would make them overwrite each other.
    """
    return [*_BASE, "-i", "pipe:0", "-c", "copy", "-f", "segment", "-segment_time", str(seconds), "-reset_timestamps", "1",
            str(folder / "%06d.ts")]  # fmt: skip


def concat_copy(parts: list[Path], dst: Path) -> Path:
    """Join whole files of one recording end to end, without re-encoding.

    This assembles a source from complete segments; it is never used to cut a clip (see ``cut_clip``).
    """
    tmp = _atomic(dst)
    listing = tmp.with_suffix(".txt")
    listing.write_text("".join("file '{}'\n".format(str(p).replace("'", "'\\''")) for p in parts))
    try:
        run([*_BASE, "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", "-movflags", "+faststart", str(tmp)], timeout=600)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    finally:
        listing.unlink(missing_ok=True)
    tmp.replace(dst)
    return dst


def make_background(cut: Path, dst: Path, *, fps: int) -> Path:
    """Small pre-blurred copy used as the vertical backdrop (cheap to decode and scale up)."""
    tmp = _atomic(dst)
    run([*_BASE, "-i", str(cut), "-an", "-vf",
         f"fps={fps},scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=12:2,eq=brightness=-0.12:saturation=1.15,format=yuv420p",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", "-g", str(fps), str(tmp)], timeout=900)  # fmt: skip
    tmp.replace(dst)
    return dst


def make_analysis_clip(src: Path, dst: Path, *, start_ms: int, end_ms: int, height: int = 360, fps: int = 6) -> Path:
    """Tiny low-fps clip (with audio) for models that accept real video input."""
    tmp = _atomic(dst)
    run([*_BASE, "-ss", ffmpeg_ts(start_ms), "-i", str(src), "-t", ffmpeg_ts(end_ms - start_ms),
         "-vf", f"fps={fps},scale=-2:{height}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
         "-ac", "1", "-c:a", "aac", "-b:a", "48k", "-movflags", "+faststart", str(tmp)], timeout=600)  # fmt: skip
    tmp.replace(dst)
    return dst


def decode_check(path: Path) -> None:
    """Fully decode the file; raises if any packet is corrupt."""
    run([*_BASE, "-xerror", "-i", str(path), "-f", "null", "-"], timeout=900)


def read_audio_mono(path: Path, *, sample_rate: int = 8000, start_ms: int = 0, duration_ms: int | None = None) -> np.ndarray:
    """Decode a (short) audio span to float32 mono — used for sync verification only."""
    require("ffmpeg")
    cmd = [*_BASE, "-ss", ffmpeg_ts(start_ms), "-i", str(path)]
    if duration_ms is not None:
        cmd += ["-t", ffmpeg_ts(duration_ms)]
    cmd += ["-vn", "-ac", "1", "-ar", str(sample_rate), "-f", "f32le", "pipe:1"]
    res = subprocess.run(cmd, capture_output=True, timeout=300, check=False)
    if res.returncode != 0:
        raise MediaError("could not decode audio", stderr=res.stderr.decode(errors="replace")[-500:])
    return np.frombuffer(res.stdout, dtype=np.float32)


def count_frames(path: Path) -> int:
    raw = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets", "-show_entries",
               "stream=nb_read_packets", "-of", "csv=p=0", str(path)], timeout=300)  # fmt: skip
    digits = raw.strip().strip(",")
    return int(digits) if digits.isdigit() else 0
