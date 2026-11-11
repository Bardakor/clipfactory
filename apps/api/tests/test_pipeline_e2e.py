"""End-to-end and media tests on generated synthetic footage.

``test_full_pipeline_with_test_adapters`` uses the FAKE analyst and FAKE
transcriber: it proves the plumbing (import → probe → signals → candidates →
cut → Remotion render with subtitles → validation → API), not AI quality.
Real media operations (FFmpeg, signal detection, Remotion) are genuinely
executed. Real transcription is covered by ``test_real_transcription``.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import zipfile
from io import BytesIO

import pytest
from fastapi.testclient import TestClient

from clipfactory import config, db
from clipfactory.domain import Caption
from clipfactory.media import ffmpeg
from clipfactory.media.validate import audio_offset_ms, validate_export
from clipfactory.pipeline.runner import claim_next_job, run_job

pytestmark = pytest.mark.slow


def test_probe_reads_real_metadata(synthetic_video):
    info = ffmpeg.probe(synthetic_video)
    assert (info.width, info.height, info.fps) == (640, 360, 30.0)
    assert info.video_codec == "h264" and info.audio_codec == "aac" and info.has_audio
    assert abs(info.duration_ms - 24_000) < 100
    assert {t["type"] for t in info.tracks} == {"video", "audio"}


def test_cut_is_frame_accurate_and_in_sync(synthetic_video, tmp_path):
    cut = ffmpeg.cut_clip(synthetic_video, tmp_path / "cut.mp4", start_ms=10_033, end_ms=15_033, fps=30, has_audio=True)
    assert ffmpeg.count_frames(cut) == 150
    assert abs(ffmpeg.probe(cut).duration_ms - 5000) <= 40
    # The loud hit sits at 12.0 s in the source → 1.967 s into the cut.
    reference = ffmpeg.read_audio_mono(synthetic_video, start_ms=10_033, duration_ms=5000)
    assert audio_offset_ms(reference, ffmpeg.read_audio_mono(cut), 8000) == 0
    shifted = ffmpeg.read_audio_mono(synthetic_video, start_ms=9_833, duration_ms=5000)
    assert audio_offset_ms(shifted, ffmpeg.read_audio_mono(cut), 8000) == -200  # the check does detect drift


def test_validation_rejects_a_bad_export(synthetic_video, tmp_path):
    result = validate_export(synthetic_video, expected_ms=24_000, width=1080, height=1920, fps=30, expect_audio=True,
                             captions=[Caption(text=" tard", startMs=30_000, endMs=31_000)])  # fmt: skip
    assert not result.ok
    assert {"dimensions", "vertical", "captions_in_range"} <= set(result.failures)
    missing = validate_export(tmp_path / "nope.mp4", expected_ms=1000, width=1080, height=1920, fps=30, expect_audio=True)
    assert missing.failures == ["file_exists"]


@pytest.fixture
def test_adapters(settings, monkeypatch):
    monkeypatch.setenv("CLIPFACTORY_AI_ADAPTER", "fake")
    monkeypatch.setenv("CLIPFACTORY_TRANSCRIBER", "fake")
    config.get_settings.cache_clear()
    db.reset_engine()
    return config.get_settings()


def test_full_pipeline_with_test_adapters(test_adapters, synthetic_video):
    settings = test_adapters
    from clipfactory.api.app import app

    with TestClient(app) as client:
        # 1. import a local fixture — 2. media probing
        up = client.post("/api/sources/upload", params={"filename": "../../sneaky name.mp4"}, content=synthetic_video.read_bytes())
        assert up.status_code == 200, up.text
        source = up.json()["source"]
        assert source["duration_ms"] > 23_000 and (source["width"], source["height"]) == (640, 360)
        assert not list(settings.path("sources").glob("*sneaky*"))  # user file names never reach the disk
        assert client.post("/api/sources/upload", params={"filename": "again.mp4"}, content=synthetic_video.read_bytes()).json()["duplicate"]

        created = client.post("/api/projects", json={"source_id": source["id"], "settings": {
            "clip_count": 1, "min_len_s": 6, "max_len_s": 12, "quality_threshold": 0, "preset": "fast", "width": 720, "height": 1280}})  # fmt: skip
        assert created.status_code == 200, created.text
        project_id = created.json()["project"]["id"]
        assert created.json()["project"]["is_test"] is True

        # 3–6. analysis through the clearly identified test adapter, candidate, render, validation
        job_id = claim_next_job("test-worker")
        assert run_job(settings, job_id, threading.Event()) == "succeeded", client.get(f"/api/jobs/{job_id}").json()

        project = client.get(f"/api/projects/{project_id}").json()["project"]
        assert [s["status"] for s in project["job"]["stages"]] == ["succeeded"] * 9
        assert project["summary"]["basis"] == "test" and project["job"]["cost"]["actual_usd"] == 0
        assert len(project["clips"]) == 1
        clip = project["clips"][0]
        assert clip["is_test"] and clip["score_basis"] == "test" and clip["render_status"] == "rendered"
        assert clip["validation"]["ok"] and clip["validation"]["checks"]["av_sync"]
        assert clip["validation"]["details"]["width"] == 720 and clip["validation"]["details"]["height"] == 1280
        assert clip["validation"]["details"]["caption_tokens"] > 0  # subtitles were rendered into the video

        # 7. the clip is served through the application, with range support for the player
        video = client.get(clip["video_url"], headers={"Range": "bytes=0-99"})
        assert video.status_code == 206 and len(video.content) == 100
        export = settings.path("exports", project_id)
        mp4 = next(export.glob("*.mp4"))
        info = ffmpeg.probe(mp4)
        assert (info.width, info.height, info.video_codec, info.audio_codec) == (720, 1280, "h264", "aac")
        assert next(export.glob("*.srt")).read_text().startswith("1\n00:00:")
        assert (export / "performance.json").exists()

        listing = client.get("/api/clips", params={"project_id": project_id}).json()["clips"]
        assert listing[0]["id"] == clip["id"]
        detail = client.get(f"/api/clips/{clip['id']}").json()
        assert detail["clip"]["caption_pages"]
        archive = client.get("/api/clips-zip", params={"project_id": project_id})
        names = zipfile.ZipFile(BytesIO(archive.content)).namelist()
        assert any(n.endswith(".mp4") for n in names) and any(n.endswith("post.txt") for n in names)

        # Editing: subtitle text + style → export marked stale → re-render reuses the cut.
        page = detail["clip"]["caption_pages"][0]
        patched = client.patch(f"/api/clips/{clip['id']}", json={"caption_edits": {page["index"]: "TEXTE CORRIGÉ"}, "subtitle_style": "clean"})
        assert patched.status_code == 200 and patched.json()["clip"]["render_status"] == "stale"
        assert patched.json()["clip"]["caption_pages"][0]["text"] == "TEXTE CORRIGÉ"
        cut_before = settings.path("work", clip["id"], "cut.mp4").stat().st_mtime
        assert client.post(f"/api/clips/{clip['id']}/render", json={"mode": "final"}).status_code == 200
        assert client.post(f"/api/clips/{clip['id']}/render", json={"mode": "final"}).status_code == 409  # already queued
        assert run_job(settings, claim_next_job("test-worker"), threading.Event()) == "succeeded"
        assert settings.path("work", clip["id"], "cut.mp4").stat().st_mtime == cut_before
        assert client.get(f"/api/clips/{clip['id']}").json()["clip"]["render_status"] == "rendered"

        # Feedback is persisted; stats keep test output separate from real work.
        assert client.post(f"/api/clips/{clip['id']}/feedback", json={"decision": "rejected", "rejection_reason": "boring"}).json()["clip"]["approval"] == "rejected"
        assert client.post(f"/api/clips/{clip['id']}/feedback", json={"decision": "rejected", "rejection_reason": "nope"}).status_code == 422
        stats = client.get("/api/stats").json()
        assert stats["clips_rendered"] == 0 and stats["sources_processed"] == 0 and stats["test_projects"] == 1

        # Deleting the source removes every derived file.
        assert client.delete(f"/api/sources/{source['id']}").status_code == 200
        assert not list(settings.path("sources").glob("*.mp4")) and not export.exists()
        assert client.get(f"/api/projects/{project_id}").status_code == 404


def test_failed_job_reports_a_useful_error(settings, tmp_path):
    from clipfactory.api.app import app

    with TestClient(app) as client:
        bad = client.post("/api/sources/upload", params={"filename": "broken.mp4"}, content=b"this is not a video")
        assert bad.status_code == 422 and bad.json()["error"]["code"] == "media_error"
        assert not list(settings.path("sources").iterdir())  # nothing left behind
        assert client.post("/api/sources/upload", params={"filename": "x.exe"}, content=b"MZ").status_code == 422
        assert client.post("/api/projects", json={"video_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}).json()["error"]["message"].startswith("confirm")


@pytest.mark.skipif(shutil.which("say") is None, reason="needs macOS speech synthesis to create real speech")
def test_real_transcription(settings, tmp_path):
    """REAL speech recognition (MLX Whisper) on synthesised speech — no adapter, no mock."""
    pytest.importorskip("mlx_whisper")
    from clipfactory.settings_store import RuntimeSettings
    from clipfactory.transcription.providers import MlxWhisperTranscriber
    from clipfactory.transcription.service import apply_corrections

    aiff, wav = tmp_path / "s.aiff", tmp_path / "s.wav"
    subprocess.run(["say", "-o", str(aiff), "The police car crashed into the wall and everyone started laughing."], check=True)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(aiff), "-ac", "1", "-ar", "16000", str(wav)], check=True)
    runtime = RuntimeSettings()
    result = MlxWhisperTranscriber(runtime.mlx_model).transcribe_chunk(wav, 0, 8000, None, "")
    segments = [apply_corrections(s, runtime.corrections) for s in result.segments]
    text = " ".join(s.text for s in segments).lower()
    assert "police" in text and "laughing" in text
    assert result.word_level and result.language == "en"
    words = [w for s in segments for w in s.words]
    assert all(a.s <= b.s for a, b in zip(words, words[1:], strict=False))  # monotonic real timestamps
    assert words[-1].e <= 8000


@pytest.mark.skipif(shutil.which("say") is None, reason="needs macOS speech synthesis to create real speech")
def test_gta6_profile_spells_game_names_in_real_transcription(settings, monkeypatch, tmp_path):
    """REAL speech recognition through the pipeline; only the AI analyst is the test adapter.
    Without the profile's vocabulary Whisper wrote "Grass Rivers" and "Mount Caliga" for this sentence."""
    pytest.importorskip("mlx_whisper")
    monkeypatch.setenv("CLIPFACTORY_AI_ADAPTER", "fake")
    config.get_settings.cache_clear()
    db.reset_engine()
    from clipfactory.api.app import app

    aiff, video = tmp_path / "s.aiff", tmp_path / "gta6.mp4"
    line = "We just left Port Gellhorn. Next stop Vice City, then the Leonida Keys, Grassrivers, Ambrosia and Mount Kalaga."
    if subprocess.run(["say", "-v", "Samantha", "-o", str(aiff), line]).returncode != 0:
        subprocess.run(["say", "-o", str(aiff), line], check=True)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30", "-i", str(aiff),
                    "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(video)], check=True)  # fmt: skip

    with TestClient(app) as client:
        source = client.post("/api/sources/upload", params={"filename": "gta6.mp4"}, content=video.read_bytes()).json()["source"]
        created = client.post("/api/projects", json={"source_id": source["id"], "settings": {"language": "en", "preset": "fast"}})
        assert created.json()["project"]["settings"]["profile"] == "gta6"  # the default profile
        job_id = claim_next_job("test-worker")
        assert run_job(config.get_settings(), job_id, threading.Event()) == "succeeded", client.get(f"/api/jobs/{job_id}").json()
        transcript = client.get(f"/api/sources/{source['id']}/transcript").json()
        text = " ".join(s["text"] for s in transcript["segments"])
        assert "Grassrivers" in text and "Mount Kalaga" in text and "Port Gellhorn" in text, text
