"""Capability-aware model registry and inference presets.

Model ids are named in exactly one place (``PRESET_MODELS``) as *preferences*.
Each is checked against OpenRouter's live catalogue before use; anything
missing or lacking the required input modality is skipped, and if nothing in
the list survives, the cheapest capable model from the catalogue is used.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from clipfactory.config import Settings
from clipfactory.domain import AI_TASKS, AITask
from clipfactory.errors import AIModelUnavailable
from clipfactory.logs import get_logger
from clipfactory.settings_store import RuntimeSettings

log = get_logger("registry")

CATALOGUE_TTL_S = 24 * 3600

REQUIRED_MODALITY: dict[AITask, str] = {
    "transcript_analysis": "text",
    "vision_image": "image",
    "vision_video": "video",
    "ranking": "text",
    "critique": "text",
    "social_caption": "text",
}

# Ordered preferences per preset, verified against the catalogue on 2026-11-11.
PRESET_MODELS: dict[str, dict[AITask, list[str]]] = {
    "fast": {
        "transcript_analysis": ["google/gemini-3.1-flash-lite", "openai/gpt-6-luna", "anthropic/claude-haiku-5.5"],
        "vision_image": ["google/gemini-3.1-flash-lite", "qwen/qwen3.8-flash"],
        "vision_video": ["google/gemini-3.1-flash-lite", "qwen/qwen3.8-flash"],
        "ranking": ["google/gemini-3.1-flash-lite", "anthropic/claude-haiku-5.5"],
        "critique": ["google/gemini-3.1-flash-lite", "anthropic/claude-haiku-5.5"],
        "social_caption": ["google/gemini-3.1-flash-lite", "anthropic/claude-haiku-5.5"],
    },
    "balanced": {
        "transcript_analysis": ["google/gemini-3.8-flash", "anthropic/claude-sonnet-5.5"],
        "vision_image": ["google/gemini-3.8-flash", "qwen/qwen3.8-max-0902"],
        "vision_video": ["google/gemini-3.8-flash", "qwen/qwen3.8-max-0902"],
        "ranking": ["anthropic/claude-sonnet-5.5", "google/gemini-4-carbon", "openai/gpt-6.1-terra"],
        "critique": ["anthropic/claude-sonnet-5.5", "google/gemini-4-carbon"],
        "social_caption": ["google/gemini-3.8-flash", "anthropic/claude-haiku-5.5"],
    },
    "max_quality": {
        "transcript_analysis": ["anthropic/claude-sonnet-5.5", "google/gemini-4-carbon"],
        "vision_image": ["google/gemini-4-carbon", "anthropic/claude-sonnet-5.5"],
        "vision_video": ["google/gemini-4-carbon", "google/gemini-3.8-flash"],
        "ranking": ["anthropic/claude-opus-5.5", "anthropic/claude-sonnet-5.5", "google/gemini-4-carbon"],
        "critique": ["anthropic/claude-opus-5.5", "anthropic/claude-sonnet-5.5"],
        "social_caption": ["anthropic/claude-sonnet-5.5", "google/gemini-3.8-flash"],
    },
}
PRESET_MODELS["custom"] = PRESET_MODELS["balanced"]


@dataclass(frozen=True)
class AnalysisBudget:
    """How much analysis a preset buys. Expensive calls are only ever spent on shortlisted candidates."""

    pool_factor: float  # candidates discovered per requested clip
    visual_factor: float  # candidates given multimodal analysis, per requested clip
    visual_cap: int
    frames_per_candidate: int
    frame_width: int
    critique: bool
    video_input: bool
    transcript_chunk_s: int = 720
    transcript_overlap_s: int = 90


BUDGETS: dict[str, AnalysisBudget] = {
    "fast": AnalysisBudget(2.5, 1.0, 10, 6, 512, critique=False, video_input=False),
    "balanced": AnalysisBudget(3.5, 2.0, 30, 8, 640, critique=True, video_input=False),
    "max_quality": AnalysisBudget(5.0, 3.0, 60, 12, 768, critique=True, video_input=True),
}
BUDGETS["custom"] = BUDGETS["balanced"]


@dataclass(frozen=True)
class ModelInfo:
    id: str
    name: str
    input_modalities: frozenset[str]
    supported_parameters: frozenset[str]
    prompt_price: float  # USD per token
    completion_price: float
    context_length: int

    @property
    def structured(self) -> bool:
        return "structured_outputs" in self.supported_parameters

    @property
    def json_mode(self) -> bool:
        return "response_format" in self.supported_parameters

    def to_public(self) -> dict:
        return {
            "id": self.id, "name": self.name, "input_modalities": sorted(self.input_modalities),
            "structured_outputs": self.structured, "context_length": self.context_length,
            "prompt_usd_per_mtok": round(self.prompt_price * 1e6, 4),
            "completion_usd_per_mtok": round(self.completion_price * 1e6, 4),
        }  # fmt: skip


def _price(value: object) -> float:
    try:
        return max(0.0, float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def parse_catalogue(payload: dict) -> dict[str, ModelInfo]:
    models: dict[str, ModelInfo] = {}
    for raw in payload.get("data", []):
        arch = raw.get("architecture") or {}
        if "text" not in (arch.get("output_modalities") or ["text"]):
            continue
        pricing = raw.get("pricing") or {}
        models[raw["id"]] = ModelInfo(
            id=raw["id"],
            name=raw.get("name") or raw["id"],
            input_modalities=frozenset(arch.get("input_modalities") or ["text"]),
            supported_parameters=frozenset(raw.get("supported_parameters") or []),
            prompt_price=_price(pricing.get("prompt")),
            completion_price=_price(pricing.get("completion")),
            context_length=int(raw.get("context_length") or 0),
        )
    return models


class ModelRegistry:
    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self._settings = settings
        self._client = client
        self._cache_file: Path = settings.path("cache", "openrouter_models.json")
        self._models: dict[str, ModelInfo] = {}
        self.fetched_at: float | None = None

    @property
    def models(self) -> dict[str, ModelInfo]:
        if not self._models:
            self.load()
        return self._models

    def load(self, *, force: bool = False) -> None:
        """Use the cached catalogue when fresh, otherwise fetch it. A stale cache beats no catalogue."""
        cached = None
        if self._cache_file.exists():
            cached = json.loads(self._cache_file.read_text())
            if not force and time.time() - cached.get("fetched_at", 0) < CATALOGUE_TTL_S:
                self._models, self.fetched_at = parse_catalogue(cached), cached.get("fetched_at")
                return
        try:
            client = self._client or httpx.Client(timeout=30)
            resp = client.get(f"{self._settings.openrouter_base_url}/models")  # public endpoint, no key sent
            resp.raise_for_status()
            payload = resp.json()
            payload["fetched_at"] = time.time()
            self._cache_file.write_text(json.dumps(payload))
            self._models, self.fetched_at = parse_catalogue(payload), payload["fetched_at"]
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("could not refresh the model catalogue", extra={"data": {"error": str(exc)}})
            if cached:
                self._models, self.fetched_at = parse_catalogue(cached), cached.get("fetched_at")

    def get(self, model_id: str) -> ModelInfo | None:
        return self.models.get(model_id)

    def supports(self, model_id: str, modality: str) -> bool:
        info = self.get(model_id)
        return info is not None and modality in info.input_modalities

    def estimate_cost(self, model_id: str, prompt_tokens: int, completion_tokens: int) -> float | None:
        info = self.get(model_id)
        if info is None:
            return None
        return prompt_tokens * info.prompt_price + completion_tokens * info.completion_price

    def resolve(self, task: AITask, runtime: RuntimeSettings, preset: str | None = None) -> list[str]:
        """Ordered model ids to try for ``task``: the routed model first, then capable fallbacks."""
        preset = preset or runtime.preset
        modality = REQUIRED_MODALITY[task]
        wanted: list[str] = []
        if runtime.custom_models.get(task):
            wanted.append(runtime.custom_models[task])
        wanted += PRESET_MODELS.get(preset, PRESET_MODELS["balanced"])[task]
        catalogue = self.models
        if not catalogue:  # offline and never fetched: trust the configured list as-is
            return list(dict.fromkeys(wanted))
        usable = [m for m in dict.fromkeys(wanted) if m in catalogue and modality in catalogue[m].input_modalities]
        if usable:
            return usable
        # Nothing preferred is usable: take the cheapest capable model, favouring structured-output support.
        capable = [
            m for m in catalogue.values()
            if modality in m.input_modalities and m.prompt_price > 0 and m.context_length >= 32_000
            and ":" not in m.id and not m.id.startswith("~")
        ]  # fmt: skip
        if not capable:
            raise AIModelUnavailable(f"no OpenRouter model accepts {modality} input for {task}", task=task)
        return [min(capable, key=lambda m: (not m.structured, m.prompt_price + m.completion_price)).id]

    def routing(self, runtime: RuntimeSettings, preset: str | None = None) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for task in AI_TASKS:
            try:
                out[task] = self.resolve(task, runtime, preset)
            except AIModelUnavailable:
                out[task] = []
        return out
