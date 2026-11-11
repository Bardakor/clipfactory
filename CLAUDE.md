# CLIPFACTORY

Local-first studio that turns long gaming footage (GTA V / GTA RP / FiveM, streamer VODs) into ranked, captioned
vertical clips. One source video in, several validated 1080×1920 MP4s out. Dashboard language: French.

Read `ARCHITECTURE.md` before changing how stages, caching or the AI gateway work.

## Layout

```
apps/api/        Python 3.12 — FastAPI, SQLite (WAL), worker, pipeline, AI gateway   (uv)
  clipfactory/
    api/           HTTP routes + serializers (read and enqueue only; never do heavy work in a request)
    pipeline/      runner (job state machine), context, stages_source / stages_editorial / stages_render
    media/         ffmpeg + ffprobe wrappers, ingest (upload, YouTube), export validation, subprocess runner
    transcription/ providers (MLX Whisper, OpenRouter STT, fake) + chunked, checkpointed service
    analysis/      audio + visual signal detection, candidates, refine (boundaries), ranking (ClipRanker)
    ai/            registry (catalogue + presets), gateway (OpenRouter), prompts, schemas, analyst
    render/        captions, layout (panels), renderer (cut → Remotion → publish)
    publishing/    tiktok.py — disabled adapter + the contract a real one must meet
  migrations/    Alembic
  tests/
apps/renderer/   Remotion composition (Clip, Captions) + render.mjs, called by the worker
apps/web/        Next.js dashboard (App Router, Tailwind, shadcn/ui, TanStack Query)
data/            sources, audio, transcripts, events, exports, previews, cache, work — gitignored
scripts/         dev.sh, check-deps.sh, test.sh, make-fixture.sh
```

## Commands

```bash
scripts/dev.sh                 # API :8000 + worker + dashboard :3000
scripts/check-deps.sh          # node, uv, ffmpeg, yt-dlp, mlx, remotion, key
scripts/test.sh                # ruff, pytest (not live), tsc, eslint, vitest
scripts/test.sh --e2e          # + Playwright against the real stack with TEST adapters
cd apps/api && uv run pytest -m "not slow and not live"   # fast loop (~2 s)
cd apps/api && uv run pytest -m live                      # real OpenRouter calls (key from .env); under a cent
cd apps/api && uv run alembic revision --autogenerate -m "…"   # after changing models.py
scripts/make-fixture.sh        # synthetic video with real synthesised FR/EN speech → data/fixtures/sample.mp4
```

## Conventions

- **Timestamps are integer milliseconds on the source timeline** everywhere (`*_ms`). Clip captions are the one
  exception: clip-relative, in `@remotion/captions`' `Caption` shape. Convert with `clipfactory.timeutil` only.
- Python: type hints everywhere, Pydantic at every boundary (API bodies, AI outputs, stage inputs), `ruff` clean.
  TypeScript: strict, no `any`.
- User-facing strings (job logs, warnings, reasons, justifications) are French. Code, comments, docs are English.
- Errors: raise a `ClipFactoryError` subclass from `errors.py` (it carries `code`, `retryable`, `details`). Never
  raise bare `Exception` from a stage; never swallow an error to keep a job green.
- Model ids are named in **one** place: `ai/registry.py::PRESET_MODELS`. Everything else resolves through the
  registry, which checks the live catalogue for the required input modality.
- A prompt change requires bumping its `*_VERSION` in `ai/prompts.py` (it is part of the cache key).
- Dependency direction: `api → pipeline → {analysis, ai, media, render, transcription} → domain/errors/timeutil`.
  `analysis`, `media`, `render` never import `ai` or `pipeline`; nothing imports `api`.

## Rules for jobs and stages

- A stage is `def stage_x(ctx: JobContext) -> None`, registered in `pipeline/runner.py::STAGES`. It must be
  **idempotent**: check for its cached artifact first, write to a temp file and rename.
- Call `ctx.should_stop()` inside every loop and pass it to subprocess helpers — that is how cancel, timeout and
  graceful shutdown all work.
- Report real progress with `ctx.progress(fraction, detail)`; pass `None` when it cannot be measured. Never invent it.
- **Never call `ctx.log / warn / metrics / progress / cache_hit` inside an open `session_scope()` that has writes
  pending.** SQLite has one writer; the nested write deadlocks for 30 s and fails the stage. Collect, close, then log.
- Keep DB transactions short. Load a detached snapshot (`load_inputs`), do the long work, then open a new session.
- A job is `succeeded` only if every stage succeeded. Interrupted jobs go back to `queued` and resume at the first
  unfinished stage.

## Media rules

- Cuts are always re-encoded (`-ss` before `-i`); stream copy is never frame-accurate and is not used.
- Never load a whole source into memory: audio in 60 s blocks, video as a piped stream of tiny frames.
- A render is published only through `publish_export`, which refuses anything that failed `validate_export`.
- Layout decisions live in Python (`render/layout.py` → panels); the Remotion composition only draws panels.
- Never fabricate: no word timing where none was measured (`timestampMs: null`), no clip, transcript line, score
  explanation or publication status that did not come from real data. Test adapters label everything `is_test`.

## Security

- `OPENROUTER_API_KEY` lives in `.env` only. It is never returned by the API, never logged (`logs.redact`), never in
  the frontend. `/api/health` exposes a boolean.
- File names from users are display metadata; on-disk names are generated ids. Resolve paths with `Settings.path()`,
  which rejects anything outside `data/`.
- YouTube: only the 11-character id is kept from user input; every request goes to a canonical URL we build.
- The API binds to 127.0.0.1 and has no authentication. Do not expose it.

## Definition of done

1. `scripts/test.sh` passes; new logic has a test at the right level (unit / mocked / e2e).
2. Anything touching media was run on a real file and the output checked (ffprobe or by eye).
3. The dashboard still starts and shows the change (`scripts/dev.sh`).
4. Docs updated: this file for conventions, `ARCHITECTURE.md` for design, `README.md` for usage and limitations.
5. A claim that something "works" names the level it was verified at: unit, mocked, test-adapter e2e, real media, or live API.
