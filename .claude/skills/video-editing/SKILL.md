---
name: video-editing
description: Use when changing how CLIPFACTORY cuts, frames or exports video - timestamp or frame conversion, FFmpeg commands, clip boundary refinement, vertical layouts and panels, facecam or crop logic, the Remotion composition, or export validation.
---

# Video editing

## Time

- One unit: **integer milliseconds on the source timeline**. Convert only through `clipfactory/timeutil.py`
  (`ms_to_frame`, `frames_for`, `ffmpeg_ts`). Never do `ms / 1000 * fps` inline.
- A clip is `[start_ms, end_ms)`. Its frame count is `frames_for(start, end, fps)`; the expected export duration
  is `frames × 1000 / fps`, not `end − start`.
- Captions are the single clip-relative structure.

## Cutting

`media/ffmpeg.py::cut_clip` is the only cut. It is frame-accurate because it **re-encodes** with `-ss` before `-i`
(decode from the previous keyframe, discard to the exact timestamp) and forces a constant frame rate.
Stream copy (`-c copy`) cuts on keyframes and desynchronises audio — it is never used. Loudness is normalised
conservatively in the same pass (EBU R128, −14 LUFS, −1.5 dBTP true peak).

The cut is cached per `(fingerprint, start, end, fps)` in `data/work/<clip>/`: restyling or relayout must not
re-cut. If you add a parameter that changes the cut, add it to the key in `renderer._ensure_cut`.

Every FFmpeg helper writes to a `.partial` file and renames; pass `should_stop` so cancellation kills the process.

## Boundaries

`analysis/refine.py` is deterministic and runs on every boundary, including ones adjusted by the critique model:
start on a sentence start (preferably after a pause) with a short lead-in; end on a sentence end, extended through
speech that resumes within 1.2 s (the reaction); snap the end to a scene cut within 0.5 s; never land inside a word;
respect min/max length, trimming setup rather than payoff.

## Layout

Python decides, Remotion draws. `render/layout.py::resolve_layout` returns segments of **panels**
(`src` normalised source rect → `x, y, w, h` output pixels) plus the caption line.

| Layout | Used when | Fallback |
| --- | --- | --- |
| `full` | default: whole gameplay on a blurred backdrop | — |
| `split` | facecam found (`auto`) or requested | `full` if no reliable facecam |
| `crop` | requested | centred if motion is not clearly concentrated |
| `dynamic` | requested | changes framing only at scene cuts ≥4 s apart; `full` if no shot qualifies |
| `manual` | user rectangle | — |

Gaming is not interview footage: do not centre-crop by default, do not move the crop within a shot, and always
record `used` + `reason` so the UI can say what happened. Keep captions inside the platform-safe band
(`SAFE_TOP`, `SAFE_BOTTOM`, `SAFE_SIDE`) and below the gameplay panel so they do not cover the HUD.

Adding a layout: a function returning `(panels, caption_y)`, a branch in `resolve_layout`, the `Layout` literal,
the French label in `apps/web/src/lib/format.ts`, and a unit test. The Remotion side should need no change.

## Remotion

Read the installed `remotion-*` skills first; this project pins Remotion 4.0.542 and uses `<Video>`/`<Audio>` from
`@remotion/media`. Composition props are written by `renderer.render_clip` and typed in `apps/renderer/src/types.ts`
— change both together. Panels inside a `<Sequence>` need `trimBefore={from}` to stay on the clip timeline. Audio
is one top-level `<Audio>` so framing changes never touch the sound.

`render.mjs` bundles once per source revision (`apps/renderer/.bundle/<hash>`), serves the clip's work directory
on loopback, and prints JSON progress lines. Preview = `--scale 0.5 --crf 28`.

## Export

A file is published only by `publish_export`, which refuses anything that failed `validate_export`: exists,
probe, H.264 + AAC, exact dimensions, vertical, frame rate, duration ±150 ms, frame count, full decode, captions
inside the duration, audio offset vs. the cut within ±80 ms. If you add an output property, add its check.

## Verify

```bash
cd apps/api && uv run pytest tests/test_pipeline_e2e.py -k "cut or validation or probe"
cd apps/api && uv run pytest tests/test_units.py -k "layout or refinement or frame"
```
Then render a real clip and look at it — extract frames with `ffmpeg -ss T -i out.mp4 -frames:v 1 f.jpg`.
