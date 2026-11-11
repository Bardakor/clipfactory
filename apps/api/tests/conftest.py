"""Shared fixtures. Every test runs against its own throw-away data directory and database."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from clipfactory import config, db


@pytest.fixture
def settings(tmp_path, monkeypatch):
    """Isolated settings: temp data dir, no API key, fake adapters off unless a test opts in."""
    monkeypatch.setenv("CLIPFACTORY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    monkeypatch.setenv("CLIPFACTORY_AI_ADAPTER", "openrouter")
    monkeypatch.setenv("CLIPFACTORY_TRANSCRIBER", "")
    config.get_settings.cache_clear()
    db.reset_engine()
    s = config.get_settings()
    db.migrate()
    yield s
    db.reset_engine()
    config.get_settings.cache_clear()


@pytest.fixture(scope="session")
def synthetic_video(tmp_path_factory) -> Path:
    """A 24 s 16:9 clip with a moving shape, tone bursts and a loud hit at 12 s. No speech, no copyright."""
    out = tmp_path_factory.mktemp("media") / "synthetic.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=24",
         "-f", "lavfi", "-i", "sine=frequency=330:duration=24",
         "-f", "lavfi", "-i", "anoisesrc=color=brown:duration=0.5:amplitude=0.9",
         "-filter_complex",
         "[1:a]volume='if(lt(mod(t,4),1.2),0.5,0.02)':eval=frame[tone];[2:a]adelay=12000:all=1[hit];"
         "[tone][hit]amix=inputs=2:normalize=0:duration=first[a]",
         "-map", "0:v", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", str(out)],
        check=True,
    )  # fmt: skip
    return out
