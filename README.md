# CLIPFACTORY

A local AI clipping studio for gaming footage. Paste a YouTube or Twitch link or drop a video; get back ranked, captioned,
vertical clips ready for TikTok, Reels and Shorts — with subtitles, suggested captions and metadata. Or leave it
plugged into Twitch channels you are authorised to clip: when their chat goes wild, the moment becomes a clip while
the stream is still running.

Built for an Apple Silicon Mac. The only external credential is an OpenRouter API key.

```
long video ─► transcribe (local) ─► audio + visual signals ─► candidates ─► AI ranking ─► cut ─► vertical render ─► validated MP4s
```

## Quick start

```bash
scripts/check-deps.sh          # verifies node, pnpm, uv, ffmpeg, yt-dlp, MLX, Remotion
cp .env.example .env           # then put your key in it:  OPENROUTER_API_KEY=sk-or-…
scripts/dev.sh                 # API + worker + live watcher + dashboard
```

Open **http://localhost:3000**.

`dev.sh` installs dependencies on first run. The first transcription downloads Whisper weights (~1.6 GB) and the
first render downloads Remotion's headless browser (~100 MB); both are cached afterwards.

Requirements: macOS on Apple Silicon, Node 20+, pnpm, [uv](https://docs.astral.sh/uv/), FFmpeg (`brew install ffmpeg uv pnpm`).

### Cost

Measured: **$0.28 for a 20-minute video on Équilibré** with 6 clips requested (21 requests) — keyframe
understanding $0.13, editorial ranking $0.11, transcript analysis $0.04, critique and social copy $0.01. The
one-minute fixture on Rapide cost under 2 cents. Vision and ranking scale with the number of clips requested;
transcript analysis scales with duration. Changing the threshold, clip count or profile weights afterwards re-uses
the cached answers and costs nothing.

A live moment (about one minute of footage, Équilibré) cost **$0.06** on the fixture: six requests per clip. A busy
night with thirty chat spikes is about $2; the per-job alert and hard cap in *Paramètres* apply to live clips too.

A model's reasoning tokens are billed as output, and OpenRouter refuses a request when the account cannot cover its
`max_tokens`, so keep a small balance rather than an exact one. *Paramètres* has a per-job cost alert and an
optional hard cap.

### Without an OpenRouter key

Everything still runs: import, local transcription, signal detection, cutting, rendering, export. Clips are then
chosen by measured signals only, and every score is labelled **Heuristique** instead of **IA**. No AI output is
ever simulated in normal operation.

## Using it

1. **Nouveau projet** — paste a YouTube URL, a Twitch VOD (`twitch.tv/videos/…`) or a Twitch clip (you must confirm
   you own or may reuse the video — a streamer's VOD is theirs: get their permission first) or drop a file
   (MP4, MOV, MKV, WebM). Pick a profile (GTA 6, GTA RP, GTA Action, Gaming général), how many clips, length range,
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

6. **Direct** — add the Twitch channels you are authorised to clip (name or link, and tick the authorisation box).
   Each one is recorded as soon as it goes live, and its chat is read without any account. The page shows who is
   talking per 10 seconds; when that number is abnormal *for that channel*, the minute around the spike
   (45 s before, 15 s after: chat reacts after the moment) goes through the normal pipeline and the clip appears
   under **Clips à poster** a couple of minutes later, with a title, a caption crediting the streamer and hashtags.
   Posting is manual: copy the caption, download the MP4. Sensitivity and window are in *Paramètres → Détection en
   direct*. Keep `scripts/dev.sh` running for as long as you want channels watched: while a channel is watched the
   Mac is kept from idle-sleeping, but closing the lid still puts it to sleep.

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
| **Real inference, Rapide preset** | live OpenRouter, full pipeline on the synthetic FR/EN fixture (7 requests, $0.018) | ✅ |
| **Real inference, Équilibré preset, on real GTA RP footage**: transcript analysis, keyframe understanding, editorial ranking (Claude Sonnet), final critique, social copy | a 20-minute CC-BY FiveM police-RP session from YouTube → 4 validated clips | ✅ |
| Insufficient-credit handling | happened for real: the job stopped at once with OpenRouter's message, and resumed later | ✅ |
| Re-ranking with new settings reuses paid answers | live: second run served from cache, $0, identical scores | ✅ |
| Qualité max preset, real video input to a model | — | ❌ not run |
| OpenRouter speech-to-text provider | mocked HTTP only | ⚠️ |
| Editorial quality | one English police-RP session, judged by reading the justifications — not a systematic evaluation; French RP only on a 3-minute extract with the Rapide preset | ⚠️ |
| **Live clipping**: chat spike detection, chat reader and reconnection, recorder start / failure / stall / resume, pending moments across a restart, pruning | automated (local fake chat server, synthetic stream, real FFmpeg); each failure-path test was checked to fail with its defect put back | ✅ |
| Live: anonymous Twitch chat counted per 10 s; streamlink → 10 s segments → joined source | two public live streams, about 45 s each: 13–37 chatters per bucket read with no login; five 1080p60 segments joined into a 41.9 s MP4 that decodes cleanly. Test footage deleted | ✅ |
| Live: moment → source → pipeline → clip to post | the running app (watcher + worker) on the fixture video injected as recorded segments, real MLX transcription and real inference: validated 1080×1920 clip in 1 min 40, caption with the channel credit and hashtag, $0.06 | ✅ |
| Live: a chat spike on a real stream to a finished clip, in one run | a live channel added by the owner (1080p60, about 5 chatters per 10 s), running app, real inference. Two moments went from chat peak to a validated 1080×1920 clip with the channel credit and no intervention: one forced by lowering the sensitivity to 1.5 (11 chatters against 5), one caught at the default 3.5 (14 against 5) — 2 min 47 and 2 min 25, $0.05 each. A first forced moment had exposed a defect (the final critique removed the moment's only clip); fixed, and that project re-run from ranking gave its clip | ✅ |
| Twitch import (VOD over HLS, clip) | a real 1080p60 VOD and a real clip from NASA's channel; the VOD through the running app to a validated clip | ✅ |
| Facecam detection | 3-minute extracts of four CC-BY GTA streams: bordered webcam and small borderless webcam measured to the pixel, cut-out streamer framed by estimate, no false detection on the stream without one | ✅ on those four |
| Streamer + game layout | a French CC-BY GTA RP stream through the full pipeline with real inference (Rapide): 3 validated clips, streamer on top; one re-rendered with the streamer at the bottom | ✅ |
| Smart crop | not exercised on real footage | ❌ |
| Sources longer than 20 minutes | — | ❌ not run (design is streaming/chunked; untested at 3 h) |

**What the real GTA RP run showed.** From 24 candidates the ranking separated real moments (an armed suspect
walking up to a police cruiser, a chaotic arrest at the Vanilla Unicorn: 57–61) from radio chatter and driving
(5–35), with justifications quoting what is actually said. Four of the five moments proposed from the transcript
ranked in the top six; windows found from signals alone mostly did not. Scores top out near 60 by design (the rubric asks
for 8+/10 to be rare), which is why the default quality threshold is 45: at 55 this session gave 2 clips, at 40 it
gave 4. On the synthetic fixture the vision model correctly reported a colour test card instead of inventing action.

**Still to do with your own footage:** real GTA VI gameplay (see Known limitations), a multi-hour source, and the
Qualité max preset.

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

Peak worker memory was ~550 MB (during transcription); Remotion's browser processes are separate.

Real GTA RP session with inference (20 min 18 s, 1080p60, 619 MB, Équilibré): **6 min 14 s** end to end for the
first two clips, 3.3× faster than real time.

| Stage | Time | Note |
| --- | --- | --- |
| Import (download + probe) | 46.8 s | |
| Transcription | 83.2 s | 229 segments, ≈15× real time |
| Signal detection | 36.6 s | ≈33× real time |
| Analysis (2 transcript sections + 12 candidates with 8 keyframes each) | 95.4 s | |
| Ranking (24 candidates), refinement + critique + social copy | 23.8 s | |
| Render | 87.1 s for 78 s of clips | 0.9× real time; this is the stage that grows with the number of clips |

Each run writes these numbers to `data/exports/<project>/performance.json`. Multi-hour sources have not been
measured yet.

## Known limitations

- **The GTA 6 profile has never seen GTA VI footage.** It was written before the game's release (19 November
  2026, PS5 / Xbox Series). Its game context, hashtags and transcription vocabulary are real (official names; on
  synthesised speech the vocabulary took name errors from 6 to 0 in English and from 6 to 1 in French), but its
  rubric weights and the default quality threshold of 45 are starting points to re-tune on the first real sessions.
- **Language is detected per ~10-minute chunk**, not per sentence. Mixed FR/EN speech is transcribed correctly in
  practice, but every segment in a chunk carries the chunk's language tag.
- **No speaker diarisation** and **no sound-event classifier** (laughter, gunshots): audio events are named for
  what is measured. Both are planned optional additions.
- **Facecam detection** was verified on four streams only. It takes one webcam position for the whole video: a
  streamer who moves or hides the webcam mid-stream gets the majority position. Round or cut-out webcams are framed
  by estimate around the face. A very small webcam (the smallest tested was 118×122 px) is found but looks soft once
  enlarged. There is no manual override of the webcam rectangle yet; when detection is wrong, use another layout.
- **Streamer + game** crops the game to its centre (about 46 % of its width): the minimap and HUD at the edges are
  not shown. Use "Gameplay complet" when they matter.
- **Twitch import**: VODs and clips only, and the whole VOD is downloaded (about 3 GB per hour at 1080p60; the
  import refuses anything over the size limit). Subscriber-only VODs are refused by Twitch.
- **Live clipping**:
  - Only channels whose streamer authorised you. The tool cannot check that; the box you tick is your statement.
  - A clip is ready about two and a half minutes after the chat peak (measured: 2 min 25 and 2 min 47; rendering a
    55 s clip is 80–95 s of it), and one worker handles jobs in order: a long VOD job that is already running
    delays live clips.
  - Chat spikes are also caused by raids, polls and "type 1 in chat": the editorial score on the clip tells you
    which ones are worth posting, and when the final check would have dropped the clip, its objection is in the
    clip's notes (`réserve du contrôle qualité`). Nothing is detected in the first two minutes of a stream.
  - Ad breaks are not recorded (streamlink skips them), so the first seconds after a channel goes live can be lost
    to the pre-roll, and a moment during a mid-roll has no footage.
  - Chat is read through Twitch's anonymous IRC access, which Twitch does not guarantee. If it stops working, the
    page shows "chat non lu" and no moment is detected.
  - The recorder keeps the last 10 minutes per channel (about 600 MB at 1080p60) in `data/live/`.
  - There is no notification when a clip is ready: keep the Direct page open.
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

1. Run your own footage: a multi-hour source, more streamers' webcam layouts.
2. Tune prompts and profile weights on real footage; use the reject reasons collected in `feedback`.
3. Sound-event classification (CLAP or YAMNet) feeding the interest curve.
4. Import only a time range of a long VOD; manual webcam rectangle in the editor; smart crop on real streams.
5. Official TikTok adapter once a developer app exists (unaudited apps can only post privately).
6. Live: a notification when a clip is ready; chat-spike detection on finished VODs; a second worker for live jobs.
