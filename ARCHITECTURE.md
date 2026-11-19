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
| **Live watcher** (`python -m clipfactory.live.watcher`) | Records enabled Twitch channels while they are live, reads their chat, turns chat spikes into pipeline jobs. Makes no clip itself. |
| **Renderer** (`node render.mjs`) | One short-lived process per render, spawned by the worker. |

SQLite in WAL mode is the only store. Media stays on disk; rows hold relative paths. The three Python processes
start together and each brings the schema to head; `db.migrate` takes a file lock so a pending migration runs once.

## Job system

- `jobs` + `job_stages` + `job_logs`. Kinds: `pipeline` (nine stages) and `render` (two stages, one clip).
- Claiming is one conditional `UPDATE … WHERE status='queued'`, so several workers on one database are safe.
- Each stage records status, measured progress, wall time, CPU seconds, RSS, cache hit and a typed error.
- Retry resets the failed stage and everything after it; earlier results are kept. `rerun` does the same from a named
  stage (e.g. new ranking settings → restart at `analyze`, reusing transcription and signals).
- Cancellation, per-stage timeout and graceful shutdown share one mechanism: `ctx.should_stop()` raises.
- **Replacing the queue later**: the worker only needs `claim_next_job`, `run_job`, `recover_interrupted_jobs`.
  A Redis-backed queue would swap the first and last; stages and the runner are untouched.

## Live clipping

```
channel live ─► streamlink ─► ffmpeg segmenter ─► data/live/<channel>/<session>/000042.ts   (10 s each, 10 min kept)
chat (IRC)   ─► distinct chatters / 10 s ─► spike vs. the last 5 min ─► live_moments (pending)
                                                                          │ window = spike −45 s … +15 s
                         whole segments of that window ─► concat ─► Source + Project + Job(priority 10) ─► the pipeline above
```

- **Recording.** `streamlink` waits for the channel to go live, skips ad segments and follows the low-latency
  playlist; it pipes into an FFmpeg segmenter that only splits (`-c copy`). A stream that ends closes the session;
  the recorder starts a new one and waits. An ad break is a gap in the footage, not an error.
- **One timeline: the wall clock.** A segment is dated by when it finished arriving (its modification time), a chat
  message by when Twitch sent it (the `tmi-sent-ts` tag). The recorder gets the video when viewers do, so "what was
  on screen when chat reacted" is a lookup by wall clock, with no mapping to stream timestamps to keep right across
  ads and restarts. Precision is a few seconds, which the window absorbs: the pipeline then picks and refines the
  clip inside it. Dating chat by send time matters after a stalled link: the backlog belongs to buckets that were
  already read and is dropped, instead of landing in one bucket and looking like a spike.
- **Chat.** One anonymous, read-only IRC connection for all channels. The signal is the number of *distinct chatters*
  per 10-second bucket: one person spamming cannot move it. A bucket not fully covered by the connection is
  unknown, not empty.
- **Spike.** `z = (chatters − median of the previous 30 buckets) / max(1.4826·MAD, √baseline)`. The Poisson floor
  keeps a chat of two people per bucket from firing on its own noise. A spike needs `z ≥ 3.5` and at least
  5 chatters, fires on the rising edge only, at most once per window length, never in the first two minutes of a
  broadcast (a stream that just started is all greetings), and only while footage is being recorded. A broadcast
  that comes back within two minutes of a drop is the same broadcast: it does not wait two minutes again.
- **Why the window starts 45 s before.** Chat reacts *after* the moment. The source handed to the pipeline is the
  minute around the spike; `stage_analyze` adds a `chat_spike` candidate ending at the end of that source, so the
  moment chat pointed at always competes with what the signals and the transcript model propose.
- **Quality threshold 0.** Every chat-detected moment yields its clip, shown with its editorial score: posting is
  manual, so the reviewer is the gate. That holds for the final critique too: at threshold 0 (live or not) it can
  still adjust a clip's boundaries, but a clip it would reject is kept, with the objection in the clip's notes
  (`réserve du contrôle qualité : …`). The post text credits the channel (`🎥 twitch.tv/<login>`, `#<login>`).
- **Failure handling.** Chat reconnects and re-joins, with a backoff that starts over after any connection that
  held, and drops a link that has been silent for 30 s (it pings every 10 s). `Recorder.poll` owns every start:
  the first one, the restart after a stream ends, the retries (5 s to 60 s apart) after one that failed; a recorder
  that failed is shown in error until a new one has stayed up 30 s. Each tick writes what it observed — recorder
  state, chat, new moments — before assembling any footage, and every recorder and every moment is handled on its
  own, so one failure costs neither another channel nor a spike. A restart finishes a pending moment from the
  segments on disk or marks it `failed` with a reason. Segments older than the buffer are deleted, except those a
  pending moment still needs. A moment with less than 10 s of real footage is not a clip.
- **One watcher, awake.** A file lock allows one watcher per data folder (a second one would stop the first one's
  recorders and clip every spike twice); segmenters orphaned by a killed watcher are stopped at the next start.
  While at least one channel is watched the watcher holds a macOS idle-sleep assertion (`caffeinate`).

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
   The final critique (`stage_refine`) may then reject a selected clip — unless the threshold is 0, which means
   "show me everything": the clip is kept and the objection is written in its notes.
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
required modality. Presets (eco / fast / balanced / max quality / custom) differ in models **and** in budget: pool
size, how many candidates get vision, frames per candidate, whether a final critique pass runs, and whether a model
looks for moments in the transcript or scores the candidates at all. `eco` turns all of that off: signals (and, on a
live project, chat) choose the clip with the heuristic score, and a model only writes its title and caption. It is
the live default, because chat has already chosen the moment.

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
| `streamlink` CLI piped into an FFmpeg segmenter | yt-dlp for live streams; our own HLS client | streamlink owns the Twitch specifics (waiting for a channel, ad segments, low latency) and is one killable process per channel. |
| Anonymous IRC for chat | EventSub `channel.chat.message` | EventSub needs a logged-in user token; reading public chat needs none. IRC without login is not a guaranteed interface: if it goes, EventSub with a login is the fallback. |
| Numbered segments dated by modification time | Segments named by `strftime` | After a network stall several segments are closed in the same second; time-based names made them overwrite each other (reproduced). |
| Distinct chatters per bucket | Raw message count | A single spammer or a bot burst is not a moment. |
| A live moment becomes an ordinary pipeline job | A dedicated fast path (cut + captions only) | One way to make a clip. The cost is about two and a half minutes from chat peak to finished clip. |
| Sources kept in their original container | Normalising every upload to one mezzanine format | Transcoding a three-hour file doubles disk use and time for no gain: FFmpeg reads the original for every derived artifact. |
