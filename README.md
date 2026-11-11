# CLIPFACTORY

A local AI clipping studio for gaming footage. Paste a YouTube URL or drop a video; get back ranked, captioned,
vertical clips ready for TikTok, Reels and Shorts — with subtitles, suggested captions and metadata.

Built for an Apple Silicon Mac. The only external credential is an OpenRouter API key.

```
long video ─► transcribe (local) ─► audio + visual signals ─► candidates ─► AI ranking ─► cut ─► vertical render ─► validated MP4s
```

## Quick start

```bash
scripts/check-deps.sh          # verifies node, pnpm, uv, ffmpeg, yt-dlp, MLX, Remotion
cp .env.example .env           # then put your key in it:  OPENROUTER_API_KEY=sk-or-…
scripts/dev.sh                 # API + worker + dashboard
```

Open **http://localhost:3000**.

`dev.sh` installs dependencies on first run. The first transcription downloads Whisper weights (~1.6 GB) and the
first render downloads Remotion's headless browser (~100 MB); both are cached afterwards.

Requirements: macOS on Apple Silicon, Node 20+, pnpm, [uv](https://docs.astral.sh/uv/), FFmpeg (`brew install ffmpeg uv pnpm`).

### Cost

The fixture run above cost under 2 cents on Rapide. A model's reasoning tokens are billed as output, and OpenRouter
refuses a request when the account cannot cover its `max_tokens`, so keep a small balance rather than an exact one.
*Paramètres* has a per-job cost alert and an optional hard cap.

### Without an OpenRouter key

Everything still runs: import, local transcription, signal detection, cutting, rendering, export. Clips are then
chosen by measured signals only, and every score is labelled **Heuristique** instead of **IA**. No AI output is
ever simulated in normal operation.

## Using it

1. **Nouveau projet** — paste a YouTube URL (you must confirm you own or may reuse the video) or drop a file
   (MP4, MOV, MKV, WebM). Pick a profile (GTA RP, GTA Action, Gaming général), how many clips, length range,
   quality threshold, AI preset, subtitle style and layout. Optional free-text instructions steer the editor model.
2. **Traitement** — watch the nine stages with measured progress, the activity log, real cost so far, and
   cancel / resume controls. A failed job keeps its finished stages; *Reprendre* restarts at the failed one.
3. **Clips** — play, approve, reject (with a reason), re-render, download the MP4 / `.srt`, copy the suggested
   caption, or download everything as a ZIP (one folder per clip: video, subtitles, metadata, `post.txt`).
4. **Éditeur** — drag the trim handles on the source timeline, edit subtitle text, switch caption style, font,
   size, colour and position, choose a layout (or a manual crop), correct the transcript, then *Aperçu rapide*
   (half-resolution) or *Rendu final*.
5. **Paramètres** — model per task (from the live OpenRouter catalogue, with prices), presets, transcription
   provider, vocabulary and correction dictionary, audio detector, concurrency, cost alert and hard cap, profile
   weights, storage cleanup.

Exports land in `data/exports/<project>/` (and optionally a folder of your choice).

The **score** on each clip is an editorial estimate of short-form potential from observable qualities (hook,
clarity, surprise, humour, payoff…). It is not a prediction of views.

## Configuration

Secrets and infrastructure are in `.env` (see `.env.example`). Everything else is edited in *Paramètres* and
stored in SQLite.

| Preset | Models (resolved against the live catalogue) | Analysis budget |
| --- | --- | --- |
| Rapide | small multimodal model for everything | fewer candidates, 6 frames each, no final critique |
| Équilibré | fast multimodal model for discovery and vision, stronger model for ranking and critique | default |
| Qualité max | strongest models, real video input when the model accepts it | most candidates, 12 frames, critique |
| Personnalisé | you choose per task | balanced budget |

## Tests

```bash
scripts/test.sh            # ruff + pytest + tsc + eslint + vitest
scripts/test.sh --e2e      # + browser test driving the real stack with TEST adapters
cd apps/api && uv run pytest -m live     # real OpenRouter calls (needs the key; costs a few cents)
```

Test adapters (`CLIPFACTORY_AI_ADAPTER=fake`, `CLIPFACTORY_TRANSCRIBER=fake`) replace OpenRouter and Whisper with
deterministic stand-ins. Anything they produce is flagged `is_test`, shown with a **TEST** badge, announced by a
banner, and excluded from dashboard statistics.

## What has been verified, and how

| Capability | Verified with | Status |
| --- | --- | --- |
| Upload, probing, fingerprint de-duplication, deletion | real files, automated | ✅ |
| YouTube import (metadata + download) | a real CC-BY video through the running app | ✅ |
| Transcription, FR + EN, word timestamps, vocabulary | real MLX Whisper on synthesised speech, automated | ✅ |
| Audio / visual signal detection, candidate discovery | real media, automated | ✅ |
| Frame-accurate cut, A/V sync check | real media; a deliberate 200 ms offset is detected | ✅ |
| Vertical render with burned-in captions, 3 styles | real Remotion renders, inspected by eye + validated | ✅ |
| Full pipeline source → validated clips | real run in heuristic mode; test-adapter run in pytest and Playwright | ✅ |
| Dashboard, gallery, editor, re-render, ZIP, feedback | browser (manual + Playwright) | ✅ |
| Restart persistence and job recovery | automated + a real restart mid-queue | ✅ |
| OpenRouter request/response handling, retries, fallbacks, budget | mocked HTTP + live calls | ✅ |
| **Real inference on the Rapide preset**: transcript analysis, keyframe understanding, editorial ranking, social copy | live OpenRouter, full pipeline on the synthetic FR/EN fixture (7 requests, $0.018 actual vs $0.019 estimated) | ✅ |
| Insufficient-credit handling | happened for real: the job stopped at once with OpenRouter's message, and resumed later | ✅ |
| Équilibré and Qualité max presets (Claude models for ranking and critique) | — | ❌ not run: the account had about 4 cents of credit |
| Final critique pass (off in Rapide) | mocked only | ⚠️ |
| OpenRouter speech-to-text provider | mocked HTTP only | ⚠️ |
| Real video input to a model | — | ❌ not run |
| Editorial quality on real GTA footage | — | ❌ not evaluated |
| Sources longer than a few minutes | — | ❌ not run (design is streaming/chunked; untested at 3 h) |

On the fixture the live run behaved as intended: it proposed the two real exchanges (the police-car crash and the
"arrested for a sandwich" line), and the vision model reported that the frames were a colour test card rather than
gameplay, lowering the visual score instead of inventing action. One observation to watch on real footage: the
small Rapide model graded two candidates with the same test-card picture very differently.

**To close the remaining gap:** add credit to the OpenRouter account (the Équilibré preset needs a few cents of
headroom per request), then process one real 20–30 minute GTA RP VOD on *Équilibré* and check (1) clips are
labelled **IA**, (2) justifications describe what actually happens, (3) *Coût réel* against your expectations,
(4) `data/exports/<project>/performance.json`.

## Measured performance

First real end-to-end run (M3 Pro, 18 GB, no OpenRouter key so no inference time): a 2 min 30 s 1080p YouTube
video → 2 validated 1080×1920 clips in **82 s** (1.8× faster than real time).

| Stage | Time | Note |
| --- | --- | --- |
| Import (download + probe) | 3.1 s | |
| Audio extraction | 0.2 s | |
| Transcription (MLX Whisper large-v3-turbo) | 15.4 s | ≈10× real time on mostly non-speech audio; ≈2× on dense speech, cold |
| Signal detection (audio + 1080p visual scan + facecam) | 5.4 s | ≈28× real time |
| Candidates, ranking, refinement | < 0.1 s | heuristic mode |
| Render (2 clips, 52 s of video) | 58.0 s | 0.9× real time |
| Validation | 1.8 s | |

Peak worker memory was ~550 MB (during transcription); Remotion's browser processes are separate. Each run writes
these numbers to `data/exports/<project>/performance.json`. Multi-hour sources and runs with inference have not
been measured yet.

## Known limitations

- **Language is detected per ~10-minute chunk**, not per sentence. Mixed FR/EN speech is transcribed correctly in
  practice, but every segment in a chunk carries the chunk's language tag.
- **No speaker diarisation** and **no sound-event classifier** (laughter, gunshots): audio events are named for
  what is measured. Both are planned optional additions.
- **Facecam detection** uses a classical face detector and a stability test. It has only been exercised on footage
  without a facecam; expect to pick the layout by hand at first.
- **Smart crop** follows motion, not semantics; it stays centred unless the motion is clearly concentrated.
- **Subtitle edits that change the word count** lose word-level highlighting for that line (the timing would
  otherwise be invented).
- **Render speed** is about real time at 1080×1920 on an M3 Pro. Hardware encoding is not used.
- **Cancelling during transcription** takes effect at the end of the current 5-minute audio chunk: an MLX
  inference in progress cannot be interrupted.
- **TikTok publishing is disabled.** `apps/api/clipfactory/publishing/tiktok.py` documents what an official
  Content Posting API integration must do. Export is the supported path.
- **Single user, no authentication.** The API listens on 127.0.0.1 only; do not expose it.
- YouTube changes often. If imports start failing, update yt-dlp: `cd apps/api && uv lock --upgrade-package yt-dlp && uv sync`.
- Remotion is free for individuals and small teams; check its licence before commercial use at scale.

## Next steps

1. Add the key, run the live tests and one real VOD (see above).
2. Tune prompts and profile weights on real footage; use the reject reasons collected in `feedback`.
3. Sound-event classification (CLAP or YAMNet) feeding the interest curve.
4. Evaluate facecam detection and smart crop on real streams.
5. Official TikTok adapter once a developer app exists.
