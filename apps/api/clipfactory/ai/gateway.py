"""Centralised OpenRouter gateway: one place for retries, fallbacks, validation, caching and cost.

Uses the Chat Completions contract (``/chat/completions``): text, ``image_url``
and ``video_url`` content parts, ``response_format`` for structured output, and
the ``usage`` object (including ``usage.cost``) that OpenRouter returns on every
response.
"""

from __future__ import annotations

import base64
import hashlib
import json
import random
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from clipfactory.ai.registry import ModelRegistry
from clipfactory.config import Settings
from clipfactory.domain import AITask
from clipfactory.errors import (
    AIAuthError,
    AIBadRequest,
    AIBudgetExceeded,
    AIError,
    AIInsufficientCredits,
    AIInvalidResponse,
    AINotConfigured,
    AIProviderError,
    AIRateLimited,
    AITimeout,
)
from clipfactory.logs import get_logger, register_secret

log = get_logger("gateway")
T = TypeVar("T", bound=BaseModel)

Part = dict[str, Any]


@dataclass
class UsageRecord:
    task: str
    model: str
    status: str  # ok | cached | error code
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd_actual: float | None = None
    cost_usd_estimated: float | None = None
    latency_ms: int = 0
    generation_id: str | None = None
    error: str | None = None
    prompt_version: str = ""
    input_fingerprint: str = ""


def text_part(text: str) -> Part:
    return {"type": "text", "text": text}


def image_part(path: Path) -> Part:
    data = base64.b64encode(path.read_bytes()).decode()
    return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{data}"}, "_sha": _sha_file(path)}


def video_part(path: Path) -> Part:
    data = base64.b64encode(path.read_bytes()).decode()
    return {"type": "video_url", "video_url": {"url": f"data:video/mp4;base64,{data}"}, "_sha": _sha_file(path)}


def _sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _wire(parts: list[Part]) -> list[Part]:
    return [{k: v for k, v in p.items() if not k.startswith("_")} for p in parts]


def _fingerprint_parts(parts: list[Part]) -> str:
    """Hash of the request content. Media is identified by file hash, never re-hashed as base64."""
    h = hashlib.sha256()
    for p in parts:
        h.update(p["type"].encode())
        h.update((p.get("_sha") or p.get("text") or "").encode())
    return h.hexdigest()


def strict_schema(model: type[BaseModel]) -> dict:
    """Pydantic schema → the conservative JSON-Schema subset providers accept in strict mode:
    refs inlined, every property required, no extra properties, no titles/defaults/bounds."""
    root = model.model_json_schema()
    defs = root.get("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, list):
            return [walk(n) for n in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            return walk(defs[node["$ref"].split("/")[-1]])
        out = {
            k: walk(v) for k, v in node.items()
            if k not in {"title", "default", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
                         "minLength", "maxLength", "minItems", "maxItems", "pattern", "$defs"}
        }  # fmt: skip
        if out.get("type") == "object":
            out["required"] = list(out.get("properties", {}).keys())
            out["additionalProperties"] = False
        return out

    return walk(root)


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def extract_json(text: str) -> Any:
    """Parse a model reply that should be JSON, tolerating code fences and stray prose around it."""
    cleaned = _FENCE.sub("", text.strip())
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start != -1 and end > start:
            return json.loads(cleaned[start : end + 1])
        raise


def classify(status: int, body: dict | str) -> AIError:
    message = ""
    if isinstance(body, dict):
        err = body.get("error") or {}
        message = str(err.get("message") or "")[:300] if isinstance(err, dict) else str(err)[:300]
    else:
        message = body[:300]
    detail = {"status": status}
    if status in (401, 403):
        return AIAuthError(f"OpenRouter rejected the API key ({status}): {message}", **detail)
    if status == 402:
        return AIInsufficientCredits(f"OpenRouter credits exhausted: {message}", **detail)
    if status == 429:
        return AIRateLimited(f"rate limited: {message}", **detail)
    if status in (408, 409, 425) or status >= 500:
        return AIProviderError(f"provider error ({status}): {message}", **detail)
    return AIBadRequest(f"request rejected ({status}): {message}", **detail)


class OpenRouterGateway:
    def __init__(
        self,
        settings: Settings,
        registry: ModelRegistry,
        *,
        concurrency: int = 4,
        timeout_s: int = 120,
        max_retries: int = 3,
        cost_warning_usd: float | None = None,
        cost_limit_usd: float | None = None,
        on_usage: Callable[[UsageRecord], None] | None = None,
        on_warning: Callable[[str], None] | None = None,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if settings.openrouter_api_key is None:
            raise AINotConfigured("OPENROUTER_API_KEY is not set")
        key = settings.openrouter_api_key.get_secret_value()
        register_secret(key)
        self.registry = registry
        self._client = client or httpx.Client(timeout=timeout_s)
        self._url = f"{settings.openrouter_base_url}/chat/completions"
        self._headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        self._timeout = timeout_s
        self._max_retries = max_retries
        self._sem = threading.BoundedSemaphore(concurrency)
        self._cache_dir = settings.path("cache", "llm")
        self._on_usage = on_usage or (lambda _r: None)
        self._on_warning = on_warning or (lambda _m: None)
        self._sleep = sleep
        self._lock = threading.Lock()
        self._warning_at = cost_warning_usd
        self._limit = cost_limit_usd
        self._warned = False
        self.spent_usd = 0.0  # sum of usage.cost actually reported by OpenRouter for this gateway's lifetime

    # ── public ──────────────────────────────────────────────────────────────

    def complete_json(
        self,
        *,
        task: AITask,
        models: list[str],
        system: str,
        user: list[Part],
        schema: type[T],
        prompt_version: str,
        max_tokens: int = 4000,
        temperature: float = 0.2,
        use_cache: bool = True,
    ) -> T:
        """Return a validated ``schema`` instance, trying each model in ``models`` in order."""
        if not models:
            raise AIBadRequest(f"no model routed for task {task}")
        input_fp = hashlib.sha256((system + _fingerprint_parts(user)).encode()).hexdigest()
        last_error: AIError | None = None
        for index, model in enumerate(models):
            cache_file = self._cache_path(model, prompt_version, schema.__name__, input_fp, temperature, max_tokens)
            if use_cache and cache_file.exists():
                try:
                    result = schema.model_validate(json.loads(cache_file.read_text())["output"])
                    self._on_usage(UsageRecord(task, model, "cached", prompt_version=prompt_version, input_fingerprint=input_fp))
                    return result
                except (ValidationError, ValueError, KeyError):
                    cache_file.unlink(missing_ok=True)
            try:
                result = self._call_model(task, model, system, user, schema, prompt_version, input_fp, max_tokens, temperature)
            except (AIAuthError, AIInsufficientCredits, AIBudgetExceeded):
                raise  # another model will not fix the account
            except AIError as exc:
                last_error = exc
                if index + 1 < len(models):
                    log.warning("model failed, trying fallback", extra={"data": {"task": task, "model": model, "error": exc.code}})
                continue
            if use_cache:
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                tmp = cache_file.with_suffix(".tmp")
                tmp.write_text(json.dumps({"model": model, "prompt_version": prompt_version, "output": result.model_dump()}))
                tmp.replace(cache_file)
            return result
        assert last_error is not None
        raise last_error

    # ── internals ───────────────────────────────────────────────────────────

    def _cache_path(self, model: str, version: str, schema: str, input_fp: str, temperature: float, max_tokens: int) -> Path:
        key = hashlib.sha256(json.dumps([model, version, schema, input_fp, temperature, max_tokens]).encode()).hexdigest()
        return self._cache_dir / key[:2] / f"{key}.json"

    def _check_budget(self) -> None:
        with self._lock:
            if self._limit is not None and self.spent_usd >= self._limit:
                raise AIBudgetExceeded(f"inference budget of ${self._limit:.2f} reached for this job", spent_usd=round(self.spent_usd, 4))

    def _record_cost(self, cost: float | None) -> None:
        if not cost:
            return
        with self._lock:
            self.spent_usd += cost
            if self._warning_at and not self._warned and self.spent_usd >= self._warning_at:
                self._warned = True
                self._on_warning(f"inference spend passed the ${self._warning_at:.2f} warning threshold (${self.spent_usd:.2f} so far)")

    def _response_formats(self, model: str, schema: type[BaseModel]) -> list[dict | None]:
        """Structured-output strategies to try, strongest first, limited to what the model advertises."""
        info = self.registry.get(model)
        formats: list[dict | None] = []
        if info is None or info.structured:
            formats.append({"type": "json_schema", "json_schema": {"name": schema.__name__, "strict": True, "schema": strict_schema(schema)}})
        if info is None or info.json_mode:
            formats.append({"type": "json_object"})
        formats.append(None)
        return formats

    def _call_model(self, task, model, system, user, schema, prompt_version, input_fp, max_tokens, temperature) -> BaseModel:  # noqa: ANN001
        schema_hint = json.dumps(strict_schema(schema))
        messages: list[dict] = [
            {"role": "system", "content": f"{system}\n\nReply with a single JSON object matching this JSON Schema, and nothing else:\n{schema_hint}"},
            {"role": "user", "content": _wire(user)},
        ]
        formats = self._response_formats(model, schema)
        repairs_left = 1
        while True:
            body: dict[str, Any] = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature}
            if formats[0] is not None:
                body["response_format"] = formats[0]
            try:
                content = self._post_with_retries(task, model, body, prompt_version, input_fp)
            except AIBadRequest:
                if len(formats) > 1:  # the provider refused this output mode: step down and try again
                    formats.pop(0)
                    continue
                raise
            try:
                return schema.model_validate(extract_json(content))
            except (ValidationError, ValueError) as exc:
                if repairs_left == 0:
                    raise AIInvalidResponse(f"{model} did not return valid JSON for {task}", error=str(exc)[:300]) from exc
                repairs_left -= 1
                messages = [
                    *messages,
                    {"role": "assistant", "content": content[:6000]},
                    {"role": "user", "content": f"That reply was not valid: {str(exc)[:500]}\nSend the corrected JSON object only."},
                ]

    def _post_with_retries(self, task: str, model: str, body: dict, prompt_version: str, input_fp: str) -> str:
        attempt = 0
        while True:
            self._check_budget()
            started = time.monotonic()
            error: AIError
            try:
                with self._sem:
                    resp = self._client.post(self._url, headers=self._headers, json=body, timeout=self._timeout)
                latency = int((time.monotonic() - started) * 1000)
                payload: dict | str
                try:
                    payload = resp.json()
                except ValueError:
                    payload = resp.text
                # OpenRouter can report an upstream failure inside a 200 response.
                embedded = payload.get("error") if isinstance(payload, dict) else None
                if resp.status_code >= 400 or embedded:
                    status = resp.status_code if resp.status_code >= 400 else int((embedded or {}).get("code") or 502)
                    error = classify(status, payload)
                    retry_after = resp.headers.get("retry-after")
                else:
                    assert isinstance(payload, dict)
                    return self._accept(task, model, payload, latency, prompt_version, input_fp)
            except httpx.TimeoutException:
                latency, retry_after = int((time.monotonic() - started) * 1000), None
                error = AITimeout(f"{model} timed out after {self._timeout}s")
            except httpx.HTTPError as exc:
                latency, retry_after = int((time.monotonic() - started) * 1000), None
                error = AIProviderError(f"network error calling OpenRouter: {type(exc).__name__}")

            self._on_usage(UsageRecord(task, model, error.code, latency_ms=latency, error=error.message[:300],
                                       prompt_version=prompt_version, input_fingerprint=input_fp))  # fmt: skip
            if not error.retryable or attempt >= self._max_retries:
                raise error
            delay = min(60.0, 2.0**attempt + random.random())
            if retry_after and retry_after.replace(".", "", 1).isdigit():
                delay = min(120.0, max(delay, float(retry_after)))
            attempt += 1
            self._sleep(delay)

    def _accept(self, task: str, model: str, payload: dict, latency: int, prompt_version: str, input_fp: str) -> str:
        usage = payload.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        actual = usage.get("cost")
        actual = float(actual) if isinstance(actual, int | float) else None
        self._record_cost(actual)
        self._on_usage(UsageRecord(
            task, model, "ok", prompt_tokens, completion_tokens, actual,
            self.registry.estimate_cost(model, prompt_tokens, completion_tokens),
            latency, payload.get("id"), prompt_version=prompt_version, input_fingerprint=input_fp,
        ))  # fmt: skip
        choices = payload.get("choices") or []
        if not choices:
            raise AIInvalidResponse(f"{model} returned no choices")
        content = (choices[0].get("message") or {}).get("content")
        if isinstance(content, list):
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        if not content:
            reason = choices[0].get("finish_reason")
            raise AIInvalidResponse(f"{model} returned an empty reply (finish_reason={reason})")
        return content
