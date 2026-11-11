"""LIVE OpenRouter tests. Skipped unless a real key is configured:

    OPENROUTER_API_KEY=... uv run pytest -m live

These spend a few cents and are the only tests that prove real inference works.
"""

from __future__ import annotations

import os
import subprocess

import pytest
from pydantic import BaseModel, SecretStr

from clipfactory.ai.gateway import OpenRouterGateway, image_part, text_part
from clipfactory.ai.registry import ModelRegistry
from clipfactory.settings_store import RuntimeSettings

KEY = os.environ.get("CLIPFACTORY_LIVE_KEY") or os.environ.get("OPENROUTER_API_KEY")
pytestmark = [pytest.mark.live, pytest.mark.skipif(not KEY, reason="no OpenRouter key configured")]


class Colour(BaseModel):
    dominant_colour: str
    confidence: float


@pytest.fixture
def live(settings):
    settings.openrouter_api_key = SecretStr(KEY)
    registry = ModelRegistry(settings)
    usage = []
    return OpenRouterGateway(settings, registry, on_usage=usage.append), registry, usage


def test_catalogue_resolves_every_task(live):
    _, registry, _ = live
    routing = registry.routing(RuntimeSettings(), "balanced")
    assert all(routing[task] for task in routing), routing


def test_structured_text_completion(live):
    gateway, registry, usage = live
    models = registry.resolve("social_caption", RuntimeSettings(), "fast")
    result = gateway.complete_json(task="social_caption", models=models, system="Answer about colours.",
                                   user=[text_part("What colour is a ripe banana?")], schema=Colour, prompt_version="live-v1", use_cache=False)  # fmt: skip
    assert "yellow" in result.dominant_colour.lower() or "jaune" in result.dominant_colour.lower()
    assert usage[-1].status == "ok" and usage[-1].prompt_tokens > 0 and usage[-1].cost_usd_actual is not None


def test_image_understanding(live, tmp_path):
    gateway, registry, _ = live
    frame = tmp_path / "red.jpg"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=320x180", "-frames:v", "1", str(frame)], check=True)
    models = registry.resolve("vision_image", RuntimeSettings(), "fast")
    result = gateway.complete_json(task="vision_image", models=models, system="Describe the image.",
                                   user=[text_part("What is the dominant colour of this frame?"), image_part(frame)],
                                   schema=Colour, prompt_version="live-v1", use_cache=False)  # fmt: skip
    assert "red" in result.dominant_colour.lower() or "rouge" in result.dominant_colour.lower()
