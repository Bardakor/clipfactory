# Architecture

CLIPFACTORY separates five kinds of work that a single multimodal model should never be asked to do at once:
deterministic media processing, cheap signal extraction, multimodal understanding, editorial judgement, rendering.

```
SOURCE ─► import ─► extract_audio ─► transcribe ─► detect_events ─►  analyze  ─► rank ─► refine ─► render ─► validate
          probe     16 kHz mono      MLX Whisper   audio novelty     candidates   Clip    speech    FFmpeg    ffprobe
          fingerpr.                  (chunked)     motion + cuts     + LLM text   Ranker  + silence cut +     decode
                                                   facecam           + vision             + cuts    Remotion  A/V sync
          └──────────── per source, cached ───────────────┘          └──────────── per project ─────────────────────┘
```

## Processes

| Process | What it does |
| --- | --- |
| **API** (FastAPI, 127.0.0.1:8000) | Reads state, validates input, enqueues jobs, serves media with range requests. Never does heavy work. |
| **Worker** (`python -m clipfactory.worker`) | Polls the `jobs` table, runs stages, heartbeats, recovers interrupted jobs on start. |
| **Dashboard** (Next.js, :3000) | Client-rendered; talks to the API directly over CORS limited to localhost. |
| **Renderer** (`node render.mjs`) | One short-lived process per render, spawned by the worker. |

SQLite in WAL mode is the only store. Media stays on disk; rows hold relative paths.

## Job system

- `jobs` + `job_stages` + `job_logs`. Kinds: `pipeline` (nine stages) and `render` (two stages, one clip).
- Claiming is one conditional `UPDATE … WHERE status='queued'`, so several workers on one database are safe.
- Each stage records status, measured progress, wall time, CPU seconds, RSS, cache hit and a typed error.
- Retry resets the failed stage and everything after it; earlier results are kept. `rerun` does the same from a named
  stage (e.g. new ranking settings → restart at `analyze`, reusing transcription and signals).
- Cancellation, per-stage timeout and graceful shutdown share one mechanism: `ctx.should_stop()` raises.
- **Replacing the queue later**: the worker only needs `claim_next_job`, `run_job`, `recover_interrupted_jobs`.
  A Redis-backed queue would swap the first and last; stages and the runner are untouched.

## Caching and invalidation

| Artifact | Key | Reused when |
| --- | --- | --- |
| Source file | fingerprint (size + 3 sampled blocks) | same file uploaded / same YouTube or Twitch id imported again |
| Analysis audio | source id | always |
| Transcript | fingerprint + provider + model + language + vocabulary + corrections | any project on the source; per-chunk checkpoints make it resumable |
| Audio signals | fingerprint + detector settings | detector settings unchanged |
| Visual scan, facecam | source id | always |
| LLM answers | model + prompt version + schema + input fingerprint + temperature + max tokens | identical request (cache hits cost nothing and are logged as such) |
| Frame-accurate cut | fingerprint + start + end + fps | restyling, relayout, caption edits — only a trim change re-cuts |

Consequences: changing subtitle colour re-renders but never re-cuts or re-transcribes; changing ranking settings
reuses extraction, transcription and signals; changing layout reuses clip timestamps and the cut.
Editing the transcript rebuilds the captions of affected clips and marks their export stale — nothing is re-analysed.

## Signals: why loud is not interesting

Audio features (RMS, spectral flux) and visual motion are converted to **novelty**: the difference from a trailing
30-second median, as a robust z-score. A long gunfight raises the baseline and stops scoring; a sudden event after
calm scores. The fused per-second interest curve gives the largest weight to *reaction*: a spike followed by speech
within three seconds. Scene cuts are detected in the same pass and removed from the motion signal.

Audio events are thresholded novelty with temporal grouping, stored with a normalised confidence. There is no
laughter/gunshot classifier: without one, naming an event "laughter" would be a guess, so events are named for what
was measured (`energy_peak`, `transient_burst`, `sudden_loudness`, `silence`).

## Candidates → ranking → selection

1. **Discovery** (broad): interest-curve peaks become setup-heavy windows; the transcript is read by an LLM in 12-minute
   sections with 90 s overlap (sections with almost no speech are never sent). Pool size is a multiple of the clips requested.
2. **Validation**: model timestamps outside the source or inverted are rejected; overlong ones keep the payoff and lose setup.
3. **Multimodal** (narrow): only the top candidates get vision analysis — adaptive keyframes (sparse for context, dense
   around the peak), or a small real video clip when the routed model's catalogue entry lists video input.
4. **ClipRanker**: `0.20·signals + 0.25·visual + 0.55·editorial` (missing parts dropped and renormalised), minus
   penalties for missing context, dead time, length, low confidence and unclear audio. Editorial is a profile-weighted
   mean of nine rubric marks. Result is 0–100, labelled an editorial estimate everywhere, never a probability.
5. **Selection**: greedy by score with temporal-overlap and text-similarity de-duplication and a small penalty for
   repeating an event type. Stops at the quality threshold; returns fewer clips than asked and says why.
6. **Feedback**: accept/reject decisions adjust an event type by at most ±5 points, only after ten decisions, with
   smoothing. The `feedback` table is append-only and snapshots what a future learned ranker would need.

Without an OpenRouter key the same pipeline runs on signals alone; scores are labelled `heuristic` in the data and the UI.

## AI gateway

One class (`ai/gateway.py`) owns every chat call: concurrency limit, timeout, exponential backoff honouring
`Retry-After`, typed error classification (including errors embedded in HTTP 200), model fallback, structured-output
negotiation (`json_schema` strict → `json_object` → prompt only, limited to what the model advertises), Pydantic
validation with one bounded repair turn, a disk cache, and usage accounting. Actual cost (`usage.cost` from
OpenRouter) and estimated cost (tokens × catalogue price) are stored in separate columns and shown separately.

`ai/registry.py` fetches the public model catalogue, and resolves each task to models that actually accept the
required modality. Presets (fast / balanced / max quality / custom) differ in models **and** in budget: pool size,
how many candidates get vision, frames per candidate, whether a final critique pass runs.

## Rendering

FFmpeg does what it is good at: a frame-accurate constant-frame-rate cut with conservative loudness normalisation
(EBU R128, −14 LUFS, −1.5 dBTP) and a tiny pre-blurred backdrop. Remotion composes: backdrop, panels, one continuous
audio track, captions. Python decides the layout as a list of panels (source rectangle → output rectangle) per time
segment, so every layout — full, crop, split, dynamic, manual — is the same composition.

Automatic layout keeps the whole gameplay visible. Tighter framing needs evidence: a facecam for split;
concentrated, stable motion for crop. Otherwise it falls back and says so.

**Facecam** (`render/layout.py::detect_facecam`, once per source, 24 frames spread over the video):

1. YuNet (OpenCV's face detector, model bundled in `clipfactory/assets/`) finds faces in every frame. The face that
   sits at the same place in at least half of them is the streamer: game characters wander, an overlay does not.
2. The webcam *window* is then measured, not guessed: per pixel, the share of frames in which it lies on an edge.
   The game changes constantly, so only overlay borders persist. The four persistent lines that best enclose the
   face are the window (`window: "edges"`), trimmed by 2 % to drop a decorative frame.
3. No rectangle (a cut-out streamer, a round frame) → a head-and-shoulders box around the face
   (`window: "estimated"`). A stable face in the middle of the screen with no window is a full-screen camera, not an
   overlay: no facecam.

**Split** stacks streamer and game edge to edge at full width, streamer on top by default (the bottom fifth of a
short is covered by the platform's own caption and buttons) or at the bottom on request. The webcam takes 26–38 % of
the height, cropped around the face if its shape does not fit; the game gets the rest, cropped to its centre.
Captions sit on the game, just past the seam.

Captions use `@remotion/captions` (`Caption`, `createTikTokStyleCaptions`). Page breaks are chosen in Python
(length per style, pauses, sentence ends) and carried by `pageBreakAfter`. Words with no measured timing have
`timestampMs: null` and are never highlighted individually. Fonts are local files, so renders work offline.

Every render is validated before it is published: probe, codec, exact dimensions, frame count, full decode, caption
range, and audio offset against the cut by envelope cross-correlation (±80 ms).

## Decisions and rejected alternatives

| Decision | Rejected | Why |
| --- | --- | --- |
| `mlx-whisper` with Whisper's own word timestamps | `whispermlx` / WhisperX forced alignment | WhisperX-style alignment adds PyTorch, wav2vec2 models per language and pyannote (Hugging Face token) for diarisation. Whisper's cross-attention word timing is measured, needs nothing extra, and was accurate on test speech. Diarisation stays optional and unimplemented. |
| Own hist/diff scan for motion and cuts | PySceneDetect | It would be a second full decode of a multi-hour file to get what the motion pass already yields. |
| No sound-event classifier in the MVP | CLAP / YAMNet now | Phase-7 item. Both need large weights and cannot tell in-game audio from the microphone; novelty signals + transcript + vision carry the MVP. |
| Layout in Python, Remotion draws panels | Layout logic in React, or everything in FFmpeg filtergraphs | Testable without rendering; one composition for all layouts; dynamic layout is just more segments. |
| Local font files via `@remotion/fonts` | `@remotion/google-fonts` | Renders must not depend on a CDN. |
| SQLite polling worker | Redis / Celery / RQ | Single machine, single user. Persistence and resumability come from the tables; the seam for a queue is three functions. |
| Chat Completions API | Responses API | Both are documented for images and video; Chat Completions is the long-stable contract for `image_url` / `video_url` parts and `response_format`. |
| Hand-written TS types beside the serializers | `packages/shared` + codegen | One consumer, ~200 lines. Revisit if a second client appears. |
| Build our own pipeline | Adapting an existing auto-clipper | **Not evaluated in depth**: no survey of existing open-source clippers was carried out, so this is a judgement call, not a comparison. The gaming requirements (no centre crop, signal-first discovery, cheap pre-filtering before any model call) drove a purpose-built design. Reviewing existing projects for reusable ideas is still open. |
| Sources kept in their original container | Normalising every upload to one mezzanine format | Transcoding a three-hour file doubles disk use and time for no gain: FFmpeg reads the original for every derived artifact. |
