---
name: quality-assurance
description: Use when testing or verifying CLIPFACTORY - writing or running tests, checking a rendered video, verifying audio/video sync, measuring performance, deciding what level of evidence a claim needs, or before saying a feature works.
---

# Quality assurance

## Levels of evidence

State which one backs a claim. They are not interchangeable.

| Level | Proves | Does not prove |
| --- | --- | --- |
| Unit (`test_units.py`, vitest) | the logic of a pure function | that media or APIs behave |
| Mocked HTTP (`test_openrouter_mocked.py`) | we send the documented request and handle each response | that real inference works |
| Test-adapter e2e (pytest, Playwright) | the plumbing: real FFmpeg, signals, Remotion, API, UI | anything about AI or transcription quality |
| Real media | that operation works on an actual file | quality on other footage |
| Live API (`-m live`) | real OpenRouter calls succeed | editorial quality |

**Never describe a mocked or fake-adapter test as proof that AI inference works.**

## Commands

```bash
scripts/test.sh                                     # everything except live + browser
scripts/test.sh --e2e                               # + Playwright (starts its own stack on :3100/:8100, TEST mode)
cd apps/api && uv run pytest -m "not slow and not live"   # ~2 s
cd apps/api && uv run pytest -m slow                # real FFmpeg/Remotion/Whisper, ~30 s
cd apps/api && uv run pytest -m live                # needs OPENROUTER_API_KEY; costs cents
```

## Where a new test goes

- Pure logic (timestamps, merging, scoring, paging, layout) → `tests/test_units.py`. No media, no DB.
- Gateway or provider behaviour → `tests/test_openrouter_mocked.py` with `httpx.MockTransport`.
- Job lifecycle → `tests/test_jobs.py` (stages replaced by recorders).
- Anything producing media → `tests/test_pipeline_e2e.py`, on the generated `synthetic_video` fixture.
- UI workflow → `apps/web/e2e/`.

Fixtures are generated, never committed: `testsrc2` + tones for structure, `scripts/make-fixture.sh` (macOS `say`)
when real speech is needed. No copyrighted footage in the repo.

## Checking a rendered video

1. `validate_export` passed — it is the gate (`clip.validation.checks`): codec, exact size, frame count, full
   decode, caption range, A/V offset.
2. `ffprobe -v error -show_entries stream=codec_name,width,height,r_frame_rate,nb_frames -of compact out.mp4`
3. Look at it: `ffmpeg -ss 2 -i out.mp4 -frames:v 1 f.jpg` at a moment someone is speaking. Right word
   highlighted? Captions inside the safe band? Gameplay fully visible? Accents rendered?
4. Sync: `details.audio_offset_ms` must be within ±80 ms. The check is real — `test_cut_is_frame_accurate_and_in_sync`
   shows it reports a deliberate 200 ms shift.

## Regression habits

- A bug found by a real run gets a test that fails without the fix (e.g. elided words, per-stage progress reset).
- SQLite "database is locked" in a stage means a `ctx.*` call inside an open write transaction — see CLAUDE.md.
- After changing a prompt or a model route, clear nothing: the cache key changes by itself. After changing an
  algorithm that writes a cached artifact, delete the artifact or version its key.

## Performance

Each pipeline run writes `data/exports/<project>/performance.json` (also `GET /api/projects/{id}/performance`):
per-stage wall time, CPU seconds, RSS, cache hits, inference requests/tokens/cost, render throughput, and source
duration ÷ processing time. Compare before and after on the same source; a stage that became a cache hit is not a
speed-up.

Measured on an M3 Pro (18 GB), short sources only: Whisper large-v3-turbo ≈ 2× real time on dense speech (cold
start included) and ≈ 10× on mostly non-speech audio; audio + visual scan of 1080p ≈ 28× real time; 1080×1920
render ≈ 0.9–1.1× real time; half-scale preview ≈ 1.7× real time. Keep `render_concurrency` at 1 unless memory allows.

## Before saying "done"

Run the command that proves it, read its output, and report the level of evidence — including what was not verified.
