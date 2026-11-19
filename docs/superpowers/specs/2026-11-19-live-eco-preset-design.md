# Live clips at minimum AI cost — design

## Why

OpenRouter credit ran out on 2026-11-19: 10 $ spent, 7.34 $ of it that day, on 129 live clips (≈5.4 ¢ each).
On a live project the chat spike has already chosen the moment, and `stage_analyze` keeps only candidates that
contain it. Most of the spend re-decides that choice:

| Call (per live clip) | Cost | What it does on a live project |
|---|---|---|
| vision_image (8 frames × 2 candidates, Gemini Flash) | 2.1 ¢ | helps choose between 3–4 windows that all hold the reaction |
| ranking (Sonnet) | 1.75 ¢ | keeps 1 of those windows; threshold 0, so it never rejects |
| transcript_analysis (Gemini Flash) | 0.7 ¢ | proposes moments over the whole source; the chat filter drops the others |
| critique (Sonnet) | 0.6 ¢ | 164 calls, 0 boundary adjustments, 0 objections |
| social_caption (Gemini Flash) | 0.3 ¢ | the post title and caption — needed |

## Goal

A live clip costs one cheap call: its title and caption. Target ≈0.04 ¢ per clip (Haiku 5.5, catalogue price
0.10 $/M in, 0.50 $/M out).

Non-goals: changing how moments are detected, the chat window, or the VOD presets' behaviour.
Anti-goal: a clip whose score, title or justification pretends to come from an AI that was not called.

## Approaches considered

1. Live channels on the existing `fast` preset — no code, ≈0.8 ¢/clip (−85 %). Keeps calls that add nothing.
2. **Chosen:** the chat chooses, AI writes only the caption (≈0.04 ¢/clip, −99 %).
3. As 2, plus 2–3 frames in the caption call so silent gameplay gets a better caption (≈0.1 ¢/clip).

## Design

- A new preset **`eco`** (« Économique ») next to `fast` / `balanced` / `max_quality`, in `domain.Preset`.
- `AnalysisBudget` gains two flags: `transcript_moments` and `editorial_ranking`. `BUDGETS["eco"]` turns them off,
  sets `visual_cap=0` and `critique=False`; every other preset keeps both flags on (unchanged behaviour).
  - `stage_analyze`: no transcript discovery when `transcript_moments` is off; no vision call when the shortlist
    is empty (today it calls `describe_visual([])`).
  - `stage_rank`: no `analyst.score` when `editorial_ranking` is off → existing heuristic path (signals, chat
    audience, heuristic justification, `score_basis="heuristic"`).
  - `stage_refine`: critique already gated by `budget.critique`; social caption unchanged.
- `PRESET_MODELS["eco"]`: text tasks → `anthropic/claude-haiku-5.5`, then `google/gemini-3.1-flash-lite`;
  vision tasks → flash-lite (listed for completeness, not called).
- `live_project_settings()` sets `preset="eco"`: every new live channel gets it.
- An Alembic data migration sets `preset` to `eco` on the existing `live_channels` rows (all are on the old
  default `balanced`; there is no UI to pick a live preset). Downgrade puts `balanced` back.
- Projects already created keep the preset they were made with (settings are copied at creation).
- Dashboard: `eco` added to the `Preset` type, both zod enums and `PRESET_LABELS`.

## What the clip loses

The AI-written justification and score (replaced by the heuristic ones, labelled as such), AI-proposed window
bounds (the chat window is the one measured on viewer clips), and some caption quality on clips with no speech.

## Verification

- Unit/mocked: an `eco` live project runs analyze → rank → refine with exactly one AI call (social_caption),
  and no vision/ranking/critique/transcript call; other presets unchanged (existing tests).
- Migration applied to the real DB; the 17 channels read `eco`.
- Live API: one real live job after credit is topped up — `inference_usage` shows a single `social_caption` row.
