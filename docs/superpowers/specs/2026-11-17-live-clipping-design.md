# Live clipping from Twitch chat spikes — design

Approved by Liam on 2026-11-17 (chat). Upload to TikTok stays manual; every clip comes with a ready-to-paste post text.

## Intent

GTA 6 launches on 2026-11-19. The first clip of a moment gets the views, and a clipper who waits for the VOD posts
late. CLIPFACTORY must stay plugged into a list of authorised Twitch channels (Marlon and friends gave permission),
record them while they are live, notice when chat is abnormally busy, and turn that moment into a finished vertical
clip in the dashboard a couple of minutes later — while the stream is still running.

## Goals

- Watch several authorised channels at once, all the time, with no login to Twitch.
- Detect a moment from chat activity that is abnormal *for that channel*, not merely high.
- Deliver the same quality of clip as the existing pipeline (captions, facecam layout, validation), unattended.
- Each clip carries title, caption (with a credit to the streamer) and hashtags, ready to copy.
- Survive what a long session throws at it: chat disconnects, stream drops, ads, restarts of the process.

## Non-goals

- Publishing to TikTok. No audited TikTok developer app exists; the adapter stays disabled. Posting is by hand.
- Clipping channels that did not give permission. A channel can only be added with the rights box ticked.
- Chat-based detection on finished VODs (TwitchDownloader-style). Possible later with the same detector.
- Push/desktop notifications when a clip is ready.

## Anti-goals

- A clip of nothing: a spike caused by one spammer, by the stream starting, or by a chat that is always busy.
- A clip that misses the moment because it starts at the spike. Chat reacts *after* the moment.
- A full disk after a six-hour stream.
- A second pipeline. Live must feed the existing stages, not duplicate them.

## Approaches considered

1. **Live recorder as a source producer (chosen).** A new process records and listens; a spike becomes a short
   source file plus an ordinary project and pipeline job. Everything downstream is the code that already exists.
   Trade-off: about two minutes from moment to finished clip, since the full pipeline runs on a ~60 s source.
2. **A dedicated fast path** (cut + captions only, no analysis). Faster by perhaps a minute, but a second pipeline
   to maintain, no editorial score, no AI title/caption, and it breaks the "one way to make a clip" rule.
3. **Chat on the finished VOD only.** Simple, but it is exactly the "post late" behaviour the feature exists to beat.

## Design

### Process

`python -m clipfactory.live.watcher`, started by `scripts/dev.sh` next to the API and the worker. It owns three things
per enabled channel: a recorder, a chat counter, a spike detector. It writes its state to SQLite; the API only reads it.

### Recording

`streamlink` (Twitch plugin: ad filtering, retry until the channel goes live) pipes into `ffmpeg -c copy -f segment`,
which writes ~10 s MPEG-TS segments named by the wall-clock second they were opened:
`data/live/<channel_id>/<session>/<epoch>.ts`. Segments older than the buffer (default 10 min) are deleted.

Wall-clock arrival time is the shared timeline: the recorder and the viewers receive the same delayed video at about
the same moment, and chat messages arrive in real time. A segment that arrived at T is what chat was reacting to at T
plus a few seconds. Ad breaks removed by streamlink are simply gaps; nothing has to be re-mapped.

### Chat

One anonymous, read-only connection to Twitch chat (IRC over TLS, `justinfan` nick, no credentials) joined to every
enabled channel. Per channel and per 10 s bucket it counts **distinct chatters** (and messages, for display).
Distinct chatters is the signal: one person spamming cannot move it.

### Spike detection (`analysis/chat.py`, pure)

For each closed bucket: baseline = median of the previous 5 minutes; scale = max(1.4826·MAD, √max(baseline, 1)) —
the Poisson floor keeps a small chat (0–3 people per bucket) from firing on noise. A spike is
`z ≥ spike_z` (default 3.5) and `chatters ≥ min_chatters` (default 5), on the rising edge only, at most one per
window length, never in the first 2 minutes of a recording (everyone says hello), and only while recording.

### From spike to clip

A spike at bucket start T0 creates a `live_moments` row (`pending`) with window `[T0 − 45 s, T0 + 15 s]`.
When segments cover the window (or 45 s after its end, whichever comes first) the whole segments overlapping it are
concatenated without re-encoding into `data/sources/<id>.mp4` — assembling whole segments is not a cut; the clip itself
is still cut frame-accurately by the renderer. Then: `Source` (kind `twitch`, channel = login, `finalize_source`),
`Project` (the channel's settings: 1 clip, 15–60 s, quality threshold 0), pipeline `Job` with priority 10.
The moment becomes `queued` and points at the project. Less than 10 s of footage → `failed` with the reason.

In `stage_analyze`, a project that came from a live moment gets one extra candidate, origin `chat_spike`, covering the
end of the source (up to the max clip length), so the moment chat pointed at always competes in the ranking.
Threshold 0 because the reviewer is the person posting: every chat-detected moment is shown, with its score.

### Post text

`stage_refine` already writes title, caption and hashtags (AI, or a labelled default). For a source with a channel it
now appends a credit line to the caption (`🎥 twitch.tv/<login>` for live; the channel name for imports) and a
`#<channel>` hashtag. No prompt changes, so no cache is invalidated. Existing copy buttons and `post.txt` pick it up.

### Data

- `live_channels`: login (unique), enabled, rights_confirmed, settings (ProjectSettings), state
  (`offline | recording | error`), state_detail, live_since, chat (recent buckets for the dashboard chart).
- `live_moments`: channel, peak_at, chatters, baseline, z, window_start/end, status
  (`pending | queued | failed`), detail, project_id.
- `RuntimeSettings.live`: spike_z, min_chatters, before_s, after_s, buffer_min.
- `AppSetting["live_heartbeat"]` for "is the watcher running?", as the worker does.

### API and dashboard

`GET /api/live`, `POST /api/live/channels`, `PATCH|DELETE /api/live/channels/{id}`. `/api/health` gains `live_alive`
and the streamlink dependency. New page **Direct** (`/direct`): add a channel (login or channel URL + rights box +
profile), per channel a state line and a 10-minute chat chart with the detected spikes, and the recent moments with
their clip card once rendered. Detection knobs sit in Paramètres with the other detector settings.

### Security

Only the login is kept from user input (`^[a-z0-9_]{3,25}$`); the URL is rebuilt. A channel is recorded only if it is
in `live_channels` with `rights_confirmed`. The VOD importer keeps refusing channel links. No Twitch credentials.

### Robustness

- Chat: PING/PONG, RECONNECT, read timeout, exponential backoff, re-join on reconnect.
- Recorder: restarted when it exits; killed and restarted when it stalls (no new segment for 60 s while recording).
- Moments are persisted before any footage is assembled; a restart finishes or fails them explicitly.
- Orphan segment folders are removed at start; child processes are terminated on shutdown.

## Calls made by the builder (not decided by Liam)

- Distinct chatters rather than raw message count as the spike signal.
- Window −45 s / +15 s, threshold z 3.5, minimum 5 chatters, 10 min buffer — all editable in Paramètres.
- Quality threshold 0 for live clips.
- Credit line appended to the caption (editable) rather than a separate field.
- Package name `clipfactory/live/`; tests in `tests/test_twitch_live.py` (the pytest marker `live` means "real OpenRouter").

## Known limits

- With ad filtering, the first seconds after going live can be lost to the pre-roll.
- Anonymous chat reading is not an officially guaranteed Twitch interface; if it breaks, EventSub needs a login.
- One worker processes jobs in order: a long VOD job already running delays live clips.

## Changed while building (see ARCHITECTURE.md for the current design)

- Segments are numbered files dated by their modification time, not files named by clock second: after a network
  stall several segments close in the same second and same-second names overwrote each other.
- streamlink 8 has no `--twitch-disable-ads` flag (it always skips ad segments); `--twitch-low-latency` was added
  because without it the stream arrives in 6–12 s bursts.
- Chat messages are dated by Twitch's send time, not by arrival, so the backlog after a stalled link cannot fake a
  spike. The dead-link limit went from 150 s to 30 s.
- The recorder's stall backstop is 10 minutes, not 60 s: an ad break writes no segment for minutes.
- Added after the independent review: spikes are persisted before footage is assembled; every recorder and moment
  is isolated; a failed recorder start is retried with backoff; a broadcast resuming within two minutes keeps its
  start; non-channel Twitch links are refused by shape; one watcher per data folder; the Mac is kept awake while a
  channel is watched; a clip is listed for posting only once its pipeline has finished.
- Found by the first run on a real live stream: the final critique rejected the only clip of a moment, which
  left the moment with no clip at all. At quality threshold 0 a critique "reject" is now a note on the clip
  (`réserve du contrôle qualité : …`), not a removal. With that fixed, two moments on the same stream went from
  chat peak to validated clip unattended, in 2 min 25 and 2 min 47 — closer to two and a half minutes than two.
