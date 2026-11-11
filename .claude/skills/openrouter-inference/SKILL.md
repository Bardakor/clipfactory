---
name: openrouter-inference
description: Use when adding or changing any call to OpenRouter in CLIPFACTORY - picking or routing a model, writing a prompt or output schema, handling an AI error, tracking cost, or debugging a failed or invalid model response.
---

# OpenRouter inference

All inference goes through `apps/api/clipfactory/ai/`. Never call OpenRouter from anywhere else.

## Before you write anything

OpenRouter's contracts and catalogue change. Re-read the docs for the endpoint you touch — do not write from memory:

- Index: `https://openrouter.ai/docs/llms.txt` (every page has a `.md` version)
- Catalogue (public, no key): `GET https://openrouter.ai/api/v1/models` — `architecture.input_modalities`,
  `supported_parameters`, `pricing`
- Transcription models: `GET /api/v1/models?output_modalities=transcription`

## Rules

1. **Never hardcode a model id outside `registry.py::PRESET_MODELS`.** Resolve with
   `registry.resolve(task, runtime, preset)`; it returns only models whose catalogue entry lists the required
   input modality, in fallback order.
2. **Never assume a capability.** Video goes only to models with `"video"` in `input_modalities`, and only via
   `analyst.describe_visual`, which falls back to keyframes on failure. Structured output is negotiated by the
   gateway from `supported_parameters`.
3. **Every model output is a Pydantic model** in `ai/schemas.py`. No defaults on fields (strict mode needs every
   property; a missing field must fail validation, not be invented). Clamp ranges in validators — strict schemas
   cannot carry `minimum`/`maximum`.
4. **Bump the prompt version** (`ai/prompts.py`) when its wording changes. It is part of the cache key and is
   stored with each `AnalysisRun`.
5. **Validate against the media, not the model.** Timestamps from a model are checked against the source duration
   before use; candidate ids it invents are discarded.
6. **Cost:** `usage.cost` from the response is the actual cost; tokens × catalogue price is the estimate. Store
   and display them separately and never call the estimate a charge. Reasoning tokens are billed as output, and
   a request is refused with 402 if the balance cannot cover `max_tokens` — keep `max_tokens` realistic.

## Adding a new AI task

1. Add the task name to `domain.AITask` / `AI_TASKS` and its modality to `registry.REQUIRED_MODALITY`.
2. Add preferences for each preset in `PRESET_MODELS` (check they exist in today's catalogue).
3. Schema in `schemas.py`, system prompt + `*_VERSION` in `prompts.py`.
4. Method on **both** `OpenRouterAnalyst` and `FakeAnalyst` (the fake must label its output `[TEST]`).
5. Mocked test in `tests/test_openrouter_mocked.py`; live test in `tests/test_live.py`.

## Request contract (Chat Completions)

```json
{"model": "<id>", "max_tokens": 4000, "temperature": 0.2,
 "messages": [{"role": "system", "content": "..."},
              {"role": "user", "content": [
                {"type": "text", "text": "..."},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}},
                {"type": "video_url", "video_url": {"url": "data:video/mp4;base64,..."}}]}],
 "response_format": {"type": "json_schema", "json_schema": {"name": "X", "strict": true, "schema": {}}}}
```

Speech-to-text is a different endpoint and shape: `POST /audio/transcriptions` with JSON
`{"model", "input_audio": {"data": "<base64>", "format": "mp3"}, "response_format": "verbose_json",
"timestamp_granularities": ["segment","word"]}`. Some models reject `verbose_json` with a 400.

## Error handling (already in the gateway — keep it there)

| Situation | Class | Behaviour |
| --- | --- | --- |
| 401 / 403 | `AIAuthError` | fail immediately, no fallback |
| 402 | `AIInsufficientCredits` | fail immediately |
| 429 | `AIRateLimited` | backoff, honour `Retry-After` |
| 408 / 5xx / `error` inside a 200 | `AIProviderError` | retry, then next model |
| 400 | `AIBadRequest` | step down the output mode, then next model |
| invalid JSON / schema | `AIInvalidResponse` | one repair turn, then next model |
| budget cap reached | `AIBudgetExceeded` | fail the stage |

In the analyst, one failed request in a batch costs only its own result; account-level errors abort the stage.

## Debugging a bad response

1. `inference_usage` rows for the job: status, tokens, error text. An `ai_invalid_response` row carries the
   validation error — read it first. (The first live run failed because `strict_schema` had stripped a *field*
   named `title` along with the `title` annotations: when a model "forgets" a field, check the schema we sent.)
2. Cached answer: `data/cache/llm/<aa>/<hash>.json`. Delete it to force a fresh call.
3. Reproduce with `tests/test_live.py` as a template (`uv run pytest -m live -k name`).
