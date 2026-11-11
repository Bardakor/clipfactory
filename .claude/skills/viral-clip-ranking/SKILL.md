---
name: viral-clip-ranking
description: Use when changing how CLIPFACTORY scores, deduplicates or selects clips - the editorial rubric and ranking prompt, profile weights, score components and penalties, the quality threshold, diversity selection, or feedback-based adjustments.
---

# Clip ranking

`analysis/ranking.py` (deterministic) + `ai/prompts.py::RANKING_SYSTEM` (the rubric a model applies).

## What the score is

An **editorial estimate** (0–100) of short-form potential from observable qualities. It is not a probability of
going viral and must never be described, labelled or displayed as one. Every clip also gets a one or two sentence
French justification stating what happens and why it works.

## How it is built

```
base      = 0.20·heuristic + 0.25·multimodal + 0.55·editorial     (missing parts dropped, rest renormalised)
penalties = missing_context·0.18 + dead_time·0.15 + length·0.10 + low_confidence·0.10 + unclear_audio·0.06
score     = 100 · clamp(base − penalties) + feedback_adjustment(±5)
```

- **heuristic** — `candidates.heuristic_score` from measured window features.
- **multimodal** — clarity, action and entertainment from the validated visual analysis, discounted by its confidence.
- **editorial** — nine rubric marks (hook, comprehensibility, surprise, humor, emotion, narrative, pacing, payoff,
  originality), 0–10 each, combined with the profile's weights.
- `basis` is `ai`, `heuristic` (no model scored it) or `test`. The UI shows it next to the number — keep it honest.

## Profiles

`PROFILE_WEIGHTS`: GTA RP favours humour, narrative, comprehensibility; GTA Action favours hook, surprise,
emotion, pacing. Users override per criterion in Settings (`runtime.profile_weights`). The profile's focus text
(`PROFILE_FOCUS`) and the user's free-text instructions go into every editorial prompt.

## Selection

`select_clips` is greedy by score, re-ranked each round:

- reject below the quality threshold;
- reject temporal IoU > 0.25 or transcript word-overlap > 0.6 with an already selected clip (duplicate);
- subtract 3 points per already-selected clip of the same event type (variety).

**Return fewer clips than requested when the source does not support them, and say why** in the explanation.
Never pad the batch with weak clips.

## Rejection criteria the rubric must enforce

Starts mid-thought with no way to understand it; ends before the punchline or reaction; depends on earlier
context; long dead time; loud but nothing happens; repeats another candidate.

## Feedback

`feedback_adjustments` nudges an event type by at most ±5 points, only with ≥10 decisions for the profile and ≥3
for the type, shrunk towards the user's overall acceptance rate. That is a transparent prior, not a model — do not
dress it up as learning. The `feedback` table is append-only and snapshots score, bounds, layout, style, reason and
optional manually entered social metrics so a real ranker can be trained and evaluated later.

## What real footage showed (one 20-minute police-RP session, Équilibré)

Strong clips scored 57–61, decent ones 43–45, radio chatter and driving 5–35. Four of the five moments proposed
from the transcript landed in the top six; signal-only windows rarely did. The scale tops out near 60 because the rubric
makes 8+/10 rare and penalties subtract ~10–15 points — hence the default threshold of 45. Treat this as one data
point, not a calibration.

`rank_briefs` is the cache key of the ranking calls. It reads only discovery-time fields; the ranking stage must
never write to anything it reads, or every re-rank is paid again (this happened once, and has a regression test).

## Changing things safely

- Weights and thresholds: change, then run `uv run pytest tests/test_units.py -k "score or selection or rubric or feedback"`.
- Rubric or prompt wording: bump `RANKING_VERSION`, keep field names in sync with `EditorialScore` and `RUBRIC`.
- New criterion: add to `RUBRIC`, `PROFILE_WEIGHTS` (all profiles), `EditorialScore`, the prompt, and the two
  label maps in `apps/web` (editor breakdown, settings sliders).
- Judge real changes on real footage: compare selected clips and justifications before and after on the same
  source (use *Relancer l'analyse*; transcription and signals are reused).
