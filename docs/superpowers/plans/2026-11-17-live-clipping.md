# Live Clipping From Chat Spikes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record authorised Twitch channels while they are live, detect abnormal chat activity, and turn each detected moment into a finished clip through the existing pipeline, with a credited post text.

**Architecture:** A new process (`clipfactory.live.watcher`) owns one recorder (streamlink → ffmpeg segments), one shared anonymous chat connection and one spike detector per channel. A spike becomes a short `Source` + `Project` + pipeline `Job`; nothing downstream is duplicated. The API only reads the state the watcher persists.

**Tech Stack:** Python 3.12 (stdlib `socket`/`ssl` for chat, `streamlink` CLI, FFmpeg), SQLAlchemy + Alembic, FastAPI, Next.js dashboard.

**Spec:** `docs/superpowers/specs/2026-11-17-live-clipping-design.md`

## Global Constraints

- Chat bucket = 10 s, aligned on epoch multiples of 10. Baseline = previous 30 buckets, at least 6 before any decision.
- Defaults: `spike_z=3.5`, `min_chatters=5`, `before_s=45`, `after_s=15`, `buffer_min=10`. No spike in the first 120 s of a recording.
- Twitch login is the only thing kept from user input: `^[a-z0-9_]{3,25}$`, lower-cased. URLs are rebuilt as `https://www.twitch.tv/<login>`.
- A channel is recorded only if `rights_confirmed` and `enabled`. No Twitch credentials anywhere.
- Timestamps in the DB are naive-UTC `datetime` (as everywhere); epoch conversion only through `clipfactory.timeutil`.
- User-facing strings are French; code, comments and docs are English. Errors are `ClipFactoryError` subclasses.
- No prompt text changes (no `*_VERSION` bump, no cache invalidation).
- Never call `ctx.log/warn/metrics/progress` inside an open `session_scope()` with pending writes.
- No commits: Liam commits on request.

## Review Focus

1. Chat disconnects for a minute, then reconnects at its normal level → no spike, baseline not polluted by fake zeros. (Task 5)
2. Spike 20 s after the minimum warm-up, or stream ends mid-window → the clip uses what was recorded; under 10 s it fails with a reason, never a crash. (Task 5)
3. Watcher restarted while a moment is `pending` → it is assembled from the segments on disk or failed explicitly. (Task 5)
4. Channel typed as `https://www.twitch.tv/Marlon/videos`, `@Marlon`, `twitch.tv/x?y`, or a foreign host → normalised to the login or refused. (Task 4)
5. Pruning never deletes a segment a pending moment still needs, and always deletes the rest past the buffer. (Task 3)

---

### Task 1: Spike detector

**Files:** Create `apps/api/clipfactory/analysis/chat.py` · Test `apps/api/tests/test_twitch_live.py`

**Interfaces — produces:**
- `BUCKET_S = 10`, `BASELINE_BUCKETS = 30`, `MIN_HISTORY = 6`
- `spike_score(history: Sequence[int], value: int) -> tuple[float, float] | None` → `(z, baseline)`; `None` under `MIN_HISTORY`. `baseline = median(history)`, `scale = max(1.4826·MAD, sqrt(max(baseline, 1)))`.
- `Spike(t0: int, chatters: int, baseline: float, z: float)` (frozen dataclass)
- `SpikeDetector.push(t0: int, chatters: int, *, spike_z: float, min_chatters: int, refractory_s: int, armed: bool) -> Spike | None` — always records history; fires on the rising edge only, when `armed`, and not within `refractory_s` of the last spike.

- [ ] Tests: `test_busy_but_steady_chat_is_not_a_spike` (40±5 forever → none), `test_sudden_jump_is_a_spike` (≈8 then 60 → z ≥ 3.5), `test_small_chat_needs_real_people` (0–2 then 4 → none; then 9 → spike), `test_no_decision_without_history` (5 buckets → None), `test_spike_fires_once_then_respects_refractory`, `test_unarmed_detector_learns_but_does_not_fire`.
- [ ] Run, see them fail; implement; run, see them pass.

### Task 2: Chat reader

**Files:** Create `apps/api/clipfactory/live/__init__.py`, `apps/api/clipfactory/live/chat.py` · Test same file as Task 1

**Interfaces — produces:**
- `parse_privmsg(line: str) -> tuple[str, str] | None` → `(channel_login, user_login)`, tolerant of an IRCv3 tag prefix.
- `ChatCounter.add(channel: str, user: str, at: float) -> None`; `ChatCounter.take(channel: str, t0: int) -> tuple[int, int]` → `(distinct chatters, messages)` of bucket `t0`, dropping it and anything older.
- `ChatClient(counter, stop: threading.Event, *, host="irc.chat.twitch.tv", port=6697, tls=True)` (a `threading.Thread`); `set_channels(logins: set[str])`; `covered(channel: str, t0: int) -> bool` (joined on the current connection since before `t0`).
- Behaviour: `NICK justinfan<random>`, JOIN/PART to match `set_channels`, answers `PING`, sends its own `PING` every 60 s, reconnects (backoff 1 → 30 s) on error, `RECONNECT`, or 150 s without data.

- [ ] Tests: `test_parse_privmsg` (plain, tagged, non-PRIVMSG → None), `test_counter_counts_distinct_chatters`, `test_chat_client_joins_counts_and_answers_ping` and `test_chat_client_reconnects_and_rejoins` against a local plain-TCP fake server (`tls=False`).
- [ ] Fail → implement → pass.

### Task 3: Recorder and segments

**Files:** Create `apps/api/clipfactory/live/recorder.py` · Modify `apps/api/clipfactory/media/ffmpeg.py` (add `concat_copy`), `apps/api/pyproject.toml` (`streamlink`), `scripts/check-deps.sh` · Test same file

**Interfaces — produces:**
- `ffmpeg.concat_copy(parts: list[Path], dst: Path) -> Path` — concat demuxer, `-c copy`, MP4 with faststart, temp + rename.
- `Segment(path: Path, start: float, end: float)`; `list_segments(session_dir: Path, *, closed: bool) -> list[Segment]` (file stem = epoch second; `end` = next start, or mtime for the last; the newest file is excluded unless `closed`).
- `window_segments(channel_dir: Path, start: float, end: float, live_session: Path | None) -> tuple[list[Segment], bool]` → segments of the session overlapping the window most, and whether more footage may still arrive.
- `prune(channel_dir: Path, *, before: float, live_session: Path | None) -> int` → deletes segments ending before `before`, removes empty closed sessions, returns bytes freed. The caller lowers `before` to the earliest pending window start.
- `Recorder(login: str, channel_dir: Path)`: `start()`, `poll(now: float) -> Literal["recording", "offline", "error"]`, `stop()`, attributes `session_dir`, `recording_since: float | None`, `detail: str`. Stall = recording and no new segment for 60 s → restart.
- Command: `python -m streamlink_cli --stdout --retry-streams 15 --twitch-disable-ads <url> 1080p60,1080p,720p60,720p,best` piped to `ffmpeg -i pipe:0 -c copy -f segment -segment_time 10 -reset_timestamps 1 -strftime 1 <session>/%s.ts`. Flags are checked against the installed `--help` before use.

- [ ] Tests: `test_segments_are_ordered_and_the_open_one_is_excluded`, `test_window_picks_the_session_that_covers_it`, `test_prune_keeps_what_is_newer_than_the_cutoff` (Review Focus 5), `test_concat_copy_joins_real_segments` (segments cut from `synthetic_video` with the same segment muxer; probe duration within 0.5 s of the sum).
- [ ] Fail → implement → pass.

### Task 4: Data model, settings, login parsing

**Files:** Modify `clipfactory/models.py`, `clipfactory/settings_store.py`, `clipfactory/config.py` (`"live"` in `DATA_SUBDIRS`), `clipfactory/timeutil.py`, `clipfactory/media/ingest.py` · Create Alembic revision

**Interfaces — produces:**
- `LiveChannel(id, login unique, enabled, rights_confirmed, settings JSON, state, state_detail, live_since, chat JSON, created_at)`
- `LiveMoment(id, channel_id FK cascade, peak_at, chatters, baseline, z, window_start, window_end, status, detail, project_id, created_at)`
- `LiveSettings(spike_z, min_chatters, before_s, after_s, buffer_min)` as `RuntimeSettings.live`
- `timeutil.from_epoch(seconds: float) -> datetime`, `timeutil.to_epoch(dt: datetime) -> float`
- `ingest.parse_twitch_login(value: str) -> str`, `ingest.twitch_channel_url(login: str) -> str`

- [ ] Tests: `test_twitch_login_is_normalised_or_refused` (Review Focus 4: `Marlon`, `@Marlon`, `https://www.twitch.tv/Marlon`, `https://twitch.tv/marlon/videos` → `marlon`; `https://evil.example/marlon`, `a b`, `videos` path-only VOD link, 26 chars → `ValidationFailed`), `test_epoch_round_trip`.
- [ ] Fail → implement → `uv run alembic revision --autogenerate -m "live channels and moments"` → pass.

### Task 5: Watcher

**Files:** Create `apps/api/clipfactory/live/watcher.py` · Modify `scripts/dev.sh` · Test same file

**Interfaces — consumes** Tasks 1–4. **Produces:**
- `Watcher(settings, shutdown, *, chat, counter, recorder_factory=Recorder)`; `tick(now: float) -> None` — reconcile recorders and chat with enabled channels, close every bucket that ended, push to detectors, create `pending` moments, materialise the ready ones, prune, persist channel state + chat series + heartbeat.
- `materialize(settings: Settings, moment_id: str, segments: list[Segment]) -> None` — concat → `Source` (`kind="twitch"`, `channel=login`, `origin_url=twitch_channel_url(login)`) → `finalize_source` → `Project` (channel settings) → `create_job(priority=10)` → moment `queued`.
- `main()`; channel default settings: `ProjectSettings(clip_count=1, min_len_s=15, max_len_s=60, quality_threshold=0, profile=…)`.

- [ ] Tests with a fake recorder and a real `ChatCounter`: `test_spike_while_recording_creates_a_pending_moment`, `test_no_moment_when_offline_or_warming_up`, `test_chat_gap_does_not_fake_a_spike` (Review Focus 1), `test_moment_becomes_a_queued_project` (real segments), `test_short_footage_fails_with_a_reason` (Review Focus 2), `test_pending_moment_survives_a_restart` (Review Focus 3), `test_removed_channel_stops_its_recorder`.
- [ ] Fail → implement → pass. Add the watcher to `scripts/dev.sh`.

### Task 6: Pipeline hooks

**Files:** Modify `clipfactory/pipeline/common.py` (`Inputs.channel`), `clipfactory/pipeline/stages_editorial.py`

**Interfaces — produces:** `credit_social(social: dict, channel: str | None, origin_url: str | None) -> dict` (appends the credit line to `caption`, adds `#<channel>` to `hashtags`, max 10, no duplicate); `stage_analyze` adds a `chat_spike` candidate ending at the end of the source for a project referenced by a `LiveMoment`.

- [ ] Tests: `test_credit_is_appended_once_and_channel_becomes_a_hashtag`, `test_no_credit_without_a_channel`, and in the existing fake-adapter e2e style `test_live_project_gets_a_chat_candidate`.
- [ ] Fail → implement → pass.

### Task 7: API

**Files:** Create `clipfactory/api/routes_live.py` · Modify `clipfactory/api/app.py`, `serializers.py`, `routes_system.py` (health `live_alive` + `streamlink`, storage `live`)

**Interfaces — produces:** `GET /api/live` → `{alive, channels[], moments[]}`; `POST /api/live/channels {channel, rights_confirmed, profile}`; `PATCH /api/live/channels/{id} {enabled?, profile?}`; `DELETE /api/live/channels/{id}`.

- [ ] Tests (FastAPI `TestClient`): `test_channel_needs_rights_and_a_valid_login`, `test_channel_cannot_be_added_twice`, `test_live_overview_lists_channels_and_moments`, `test_channel_can_be_paused_and_removed`.
- [ ] Fail → implement → pass.

### Task 8: Dashboard

**Files:** Create `apps/web/src/app/direct/page.tsx` · Modify `src/lib/types.ts`, `src/lib/queries.ts`, `src/components/shell.tsx`, `src/app/parametres/page.tsx`

- [ ] Page **Direct**: add-channel form (login or link, profile, rights box), per-channel state line and 10-minute chat chart with spike marks, moments list with the existing `ClipCard` once a clip exists. Nav entry and a "Direct" system status line. Detection settings card in Paramètres.
- [ ] `pnpm typecheck && pnpm lint && pnpm test` pass; the page is checked in the browser.

### Task 9: Real stream and docs

- [ ] Run the watcher on a channel that is live, with a lowered threshold, until one moment goes through the real pipeline; check the export with ffprobe and by eye. Delete the test footage.
- [ ] Update `CLAUDE.md` (layout, commands, security rule), `ARCHITECTURE.md` (live section), `README.md` (usage, limits).
- [ ] `scripts/test.sh` passes.
