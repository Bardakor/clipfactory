"""OpenRouter integration against a MOCKED HTTP transport.

These tests prove that our client speaks the documented request/response
contract and handles failures correctly. They do NOT prove that real inference
works — that is ``test_live.py``, which needs a real key.
"""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import BaseModel, SecretStr

from clipfactory.ai.gateway import OpenRouterGateway, extract_json, strict_schema, text_part
from clipfactory.ai.registry import ModelRegistry, parse_catalogue
from clipfactory.ai.schemas import EditorialScores, MomentList
from clipfactory.errors import AIAuthError, AIBudgetExceeded, AIInvalidResponse, AINotConfigured, AIProviderError
from clipfactory.settings_store import RuntimeSettings
from clipfactory.transcription.providers import OpenRouterTranscriber

CATALOGUE = {"data": [
    {"id": "acme/text-pro", "name": "Text Pro", "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
     "supported_parameters": ["structured_outputs", "response_format"], "pricing": {"prompt": "0.000001", "completion": "0.000002"}, "context_length": 200000},
    {"id": "acme/vision", "name": "Vision", "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]},
     "supported_parameters": ["structured_outputs", "response_format"], "pricing": {"prompt": "0.000003", "completion": "0.000004"}, "context_length": 200000},
    {"id": "acme/video", "name": "Video", "architecture": {"input_modalities": ["text", "image", "video"], "output_modalities": ["text"]},
     "supported_parameters": ["response_format"], "pricing": {"prompt": "0.000005", "completion": "0.000006"}, "context_length": 1000000},
    {"id": "acme/image-gen", "name": "Image generator", "architecture": {"input_modalities": ["text"], "output_modalities": ["image"]},
     "supported_parameters": [], "pricing": {"prompt": "0", "completion": "0"}, "context_length": 1000},
]}  # fmt: skip


class Answer(BaseModel):
    value: int
    label: str


def reply(content, *, cost=0.0012, status=200, prompt=100, completion=20):
    body = {"id": "gen-1", "model": "acme/text-pro", "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": prompt, "completion_tokens": completion, "cost": cost}}  # fmt: skip
    return httpx.Response(status, json=body)


@pytest.fixture
def keyed(settings):
    settings.openrouter_api_key = SecretStr("sk-or-test-0000000000")
    return settings


def make_gateway(keyed, handler, **kw):
    registry = ModelRegistry(keyed)
    registry._models = parse_catalogue(CATALOGUE)
    usage, warnings, slept = [], [], []
    gateway = OpenRouterGateway(keyed, registry, client=httpx.Client(transport=httpx.MockTransport(handler)), on_usage=usage.append,
                                on_warning=warnings.append, sleep=slept.append, **kw)  # fmt: skip
    return gateway, usage, warnings, slept


def ask(gateway, models=("acme/text-pro",), **kw):
    return gateway.complete_json(task="ranking", models=list(models), system="sys", user=[text_part("question")], schema=Answer, prompt_version="t-v1", **kw)


def test_request_shape_and_usage_accounting(keyed):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"], seen["auth"], seen["body"] = str(request.url), request.headers["authorization"], json.loads(request.content)
        return reply('{"value": 7, "label": "ok"}')

    gateway, usage, _, _ = make_gateway(keyed, handler)
    assert ask(gateway) == Answer(value=7, label="ok")
    assert seen["url"].endswith("/chat/completions") and seen["auth"] == "Bearer sk-or-test-0000000000"
    body = seen["body"]
    assert body["model"] == "acme/text-pro" and body["messages"][1]["content"] == [{"type": "text", "text": "question"}]
    fmt = body["response_format"]
    assert fmt["type"] == "json_schema" and fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["schema"]["additionalProperties"] is False
    record = usage[-1]
    assert record.status == "ok" and (record.prompt_tokens, record.completion_tokens) == (100, 20)
    assert record.cost_usd_actual == 0.0012  # what OpenRouter reported
    assert record.cost_usd_estimated == pytest.approx(100 * 1e-6 + 20 * 2e-6)  # tokens × catalogue price, kept separate
    assert gateway.spent_usd == 0.0012


def test_retries_rate_limits_with_backoff(keyed):
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429, json={"error": {"message": "slow down"}}, headers={"retry-after": "7"})
        return reply('{"value": 1, "label": "a"}')

    gateway, usage, _, slept = make_gateway(keyed, handler)
    assert ask(gateway).value == 1
    assert len(calls) == 3 and slept == [7.0, 7.0]  # honours Retry-After
    assert [u.status for u in usage] == ["ai_rate_limited", "ai_rate_limited", "ok"]


def test_falls_back_to_next_model(keyed):
    def handler(request):
        model = json.loads(request.content)["model"]
        return httpx.Response(503, json={"error": {"message": "down"}}) if model == "acme/text-pro" else reply('{"value": 2, "label": "b"}')

    gateway, usage, _, _ = make_gateway(keyed, handler, max_retries=1)
    assert ask(gateway, models=("acme/text-pro", "acme/vision")).value == 2
    assert [u.model for u in usage if u.status == "ok"] == ["acme/vision"]


def test_error_embedded_in_200_is_a_failure(keyed):
    gateway, _, _, _ = make_gateway(keyed, lambda r: httpx.Response(200, json={"error": {"code": 502, "message": "upstream died"}}), max_retries=0)
    with pytest.raises(AIProviderError):
        ask(gateway)


def test_invalid_json_gets_one_bounded_repair(keyed):
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return reply("Sure! Here you go: not json" if len(bodies) == 1 else '```json\n{"value": 3, "label": "fixed"}\n```')

    gateway, _, _, _ = make_gateway(keyed, handler)
    assert ask(gateway).label == "fixed"
    assert len(bodies) == 2 and "not valid" in bodies[1]["messages"][-1]["content"]

    gateway, _, _, _ = make_gateway(keyed, lambda r: reply('{"value": "NaN"}'))
    with pytest.raises(AIInvalidResponse):
        ask(gateway, use_cache=False)


def test_steps_down_when_structured_mode_is_refused(keyed):
    formats = []

    def handler(request):
        fmt = json.loads(request.content).get("response_format")
        formats.append(fmt["type"] if fmt else None)
        return httpx.Response(400, json={"error": {"message": "unsupported"}}) if fmt and fmt["type"] == "json_schema" else reply('{"value": 4, "label": "d"}')

    gateway, _, _, _ = make_gateway(keyed, handler)
    assert ask(gateway).value == 4 and formats == ["json_schema", "json_object"]
    # A model that does not advertise structured outputs is never sent a json_schema in the first place.
    formats.clear()
    assert ask(gateway, models=("acme/video",), use_cache=False).value == 4 and formats == ["json_object"]


def test_auth_errors_are_not_retried_or_fallen_back(keyed):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(401, json={"error": {"message": "No auth credentials found"}})

    gateway, _, _, slept = make_gateway(keyed, handler)
    with pytest.raises(AIAuthError):
        ask(gateway, models=("acme/text-pro", "acme/vision"))
    assert len(calls) == 1 and slept == []


def test_cache_is_keyed_by_model_prompt_version_and_input(keyed):
    calls = []

    def handler(request):
        calls.append(1)
        return reply('{"value": 5, "label": "e"}')

    gateway, usage, _, _ = make_gateway(keyed, handler)
    ask(gateway)
    ask(gateway)
    assert len(calls) == 1 and usage[-1].status == "cached" and gateway.spent_usd == 0.0012  # a cache hit costs nothing
    gateway.complete_json(task="ranking", models=["acme/text-pro"], system="sys", user=[text_part("question")], schema=Answer, prompt_version="t-v2")
    gateway.complete_json(task="ranking", models=["acme/text-pro"], system="sys", user=[text_part("another")], schema=Answer, prompt_version="t-v1")
    assert len(calls) == 3


def test_budget_warning_and_hard_limit(keyed):
    gateway, _, warnings, _ = make_gateway(keyed, lambda r: reply('{"value": 6, "label": "f"}', cost=0.6), cost_warning_usd=0.5, cost_limit_usd=1.0)
    ask(gateway, use_cache=False)
    assert len(warnings) == 1 and "0.50" in warnings[0]
    ask(gateway, use_cache=False)
    with pytest.raises(AIBudgetExceeded):
        ask(gateway, use_cache=False)


def test_gateway_requires_a_key(settings):
    with pytest.raises(AINotConfigured):
        OpenRouterGateway(settings, ModelRegistry(settings))


def test_strict_schema_and_json_extraction():
    schema = strict_schema(EditorialScores)
    item = schema["properties"]["scores"]["items"]
    assert "$ref" not in json.dumps(schema) and item["additionalProperties"] is False
    assert set(item["required"]) == set(item["properties"]) and "title" not in item
    assert extract_json('prefix {"a": 1} suffix') == {"a": 1}
    assert extract_json('```json\n{"a": 2}\n```') == {"a": 2}
    clamped = MomentList.model_validate({"moments": [{"start_s": 1, "end_s": 9, "title": "t", "event_type": "e", "setup": "", "payoff": "",
                                                      "why_entertaining": "", "confidence": 7}]})  # fmt: skip
    assert clamped.moments[0].confidence == 1.0


def test_registry_is_capability_aware(keyed):
    registry = ModelRegistry(keyed)
    registry._models = parse_catalogue(CATALOGUE)
    assert "acme/image-gen" not in registry.models  # cannot produce text
    runtime = RuntimeSettings(custom_models={"vision_video": "acme/vision", "ranking": "acme/text-pro"})
    assert registry.resolve("ranking", runtime)[0] == "acme/text-pro"
    # The user routed a model without video input to the video task: it is skipped, not trusted.
    assert registry.resolve("vision_video", runtime) == ["acme/video"]
    assert registry.resolve("vision_image", RuntimeSettings()) == ["acme/vision"]  # cheapest capable structured model
    assert registry.estimate_cost("acme/vision", 1000, 1000) == pytest.approx(0.007)
    assert registry.estimate_cost("unknown/model", 1, 1) is None


def test_registry_refresh_uses_public_endpoint_and_caches(keyed):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=CATALOGUE)

    registry = ModelRegistry(keyed, client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert len(registry.models) == 3
    assert "authorization" not in calls[0].headers  # the catalogue is public: the key is not sent
    ModelRegistry(keyed, client=httpx.Client(transport=httpx.MockTransport(handler))).load()
    assert len(calls) == 1  # second registry read the on-disk cache


def test_stt_uses_json_base64_contract_and_falls_back(keyed, tmp_path, monkeypatch):
    mp3 = tmp_path / "chunk.mp3"
    mp3.write_bytes(b"fake-mp3")
    monkeypatch.setattr("clipfactory.transcription.providers.ffmpeg.encode_audio_chunk", lambda *a, **k: (mp3.write_bytes(b"fake-mp3"), mp3)[1])
    bodies = []

    def verbose(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"language": "fr", "text": "bonjour à tous", "usage": {"seconds": 2.0, "cost": 0.0003},
                                         "segments": [{"start": 0.0, "end": 1.5, "text": "bonjour à tous"}],
                                         "words": [{"word": "bonjour", "start": 0.0, "end": 0.6}, {"word": "à", "start": 0.6, "end": 0.8},
                                                   {"word": "tous", "start": 0.8, "end": 1.5}]})  # fmt: skip

    client = httpx.Client(base_url=keyed.openrouter_base_url, transport=httpx.MockTransport(verbose))
    result = OpenRouterTranscriber(keyed, "acme/stt", client=client).transcribe_chunk(tmp_path / "a.wav", 60_000, 90_000, None, "")
    body = bodies[0]
    assert set(body["input_audio"]) == {"data", "format"} and body["input_audio"]["format"] == "mp3"
    assert body["response_format"] == "verbose_json" and body["timestamp_granularities"] == ["segment", "word"]
    assert result.word_level and result.cost_usd == 0.0003
    assert [(w.w, w.s, w.e) for w in result.segments[0].words] == [(" bonjour", 60_000, 60_600), (" à", 60_600, 60_800), (" tous", 60_800, 61_500)]

    def plain_only(request):
        if "response_format" in json.loads(request.content):
            return httpx.Response(400, json={"error": {"message": "verbose_json not supported"}})
        return httpx.Response(200, json={"text": "bonjour à tous", "usage": {"seconds": 2.0}})

    client = httpx.Client(base_url=keyed.openrouter_base_url, transport=httpx.MockTransport(plain_only))
    transcriber = OpenRouterTranscriber(keyed, "acme/stt-plain", client=client)
    result = transcriber.transcribe_chunk(tmp_path / "a.wav", 0, 30_000, "fr", "")
    assert not result.word_level and result.segments[0].words is None  # no timing invented
    assert transcriber.chunk_ms == 30_000  # shorter chunks from now on, since each is one untimed block
