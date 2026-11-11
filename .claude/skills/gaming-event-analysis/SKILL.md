---
name: gaming-event-analysis
description: Use when changing how CLIPFACTORY finds interesting moments in gaming footage - audio features and events, the visual motion or scene-cut scan, speech statistics, the fused interest curve, candidate windows, or frame sampling for vision models.
---

# Gaming event analysis

Three independent evidence streams are extracted cheaply over the whole video, then fused. Expensive model calls
only ever see what this stage shortlists.

| Stream | Module | Output |
| --- | --- | --- |
| Audio | `analysis/audio.py` | per-100 ms loudness, energy novelty, flux novelty, silence |
| Visual | `analysis/visual.py` | per-500 ms motion, motion novelty, scene-cut timestamps |
| Speech | `analysis/audio.py::speech_stats` | per-second activity, word density, turn rate |
| Fusion | `analysis/candidates.py` | per-second interest curve → seeds → windows → features |

## The principle: novelty, not level

A high volume is not an interesting moment. Every signal is `value − trailing 30 s median`, as a robust z-score.
Sustained gunfire lifts its own baseline and stops scoring; an impact after calm scores. If you add a signal,
express it the same way — never feed a raw level into the interest curve.

The strongest term in the curve is **reaction**: a spike in any stream followed by speech within three seconds.
That is what separates "something happened and someone reacted" from "something was loud".

## Rules

- **Constant memory.** Audio is read in 60 s blocks (`sf.blocks`); video arrives as a pipe of 160×90 grayscale
  frames at 2 fps. Never decode a whole source into an array.
- **Scene cuts are not motion.** `analyse_motion` replaces cut frames with the local baseline before computing novelty.
- **Name events for what was measured.** `energy_peak`, `transient_burst`, `sudden_loudness`, `silence`. Do not
  label something "laughter" or "gunshot" without a classifier, and remember a classifier cannot tell game audio
  from the microphone.
- **Group in time.** Detections closer than `group_gap_ms` are one event. Confidence is 0–1 (0.5 at threshold).
- **Windows carry their setup.** `window_around` puts ~55 % of the window before the peak.
- **Everything is cached per source** (`data/events/<source>/`). Changing a detector setting changes the audio
  cache key; changing the algorithm means clearing those files or adding a version to the key.

## Frame sampling for vision models

`stages_editorial._frame_times`: 40 % of frames spread across the window for context, 60 % within ±3 s of the
interest peak, minimum 300 ms apart. Count and width come from the preset budget. Each frame is sent with its
relative and absolute time label. Raise the budget in `registry.BUDGETS`, not ad hoc.

## Adding a signal (e.g. a sound classifier)

1. Produce a per-second or per-hop array + event rows; cache it in `data/events/<source>/`.
2. Convert to novelty or a 0–1 confidence; add it to `build_signals` with an explicit weight (weights sum to 1).
3. Add its summary to `_signal_notes` so text models see it.
4. Unit-test on synthetic arrays (see `test_sustained_loudness_has_no_novelty`), then run
   `scripts/make-fixture.sh` and check the seeds land on the "crash" at 21 s.

## Checking your change on real media

```bash
scripts/make-fixture.sh
cd apps/api && uv run pytest tests/test_units.py -k "novelty or events or seeds or cut"
```
Then process the fixture from the dashboard and read the `analyze` log lines and the candidate list on the project page.
