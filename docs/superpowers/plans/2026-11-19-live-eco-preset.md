# Live eco preset Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A live clip costs one AI call (title + caption on Haiku 5.5) instead of five.

**Architecture:** A new `eco` preset. Its `AnalysisBudget` turns off transcript discovery, vision, editorial
ranking and the final critique; the stages read those flags. Live channels default to it, and a data migration
moves the existing ones.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy / Alembic, Next.js + zod.

**Spec:** `docs/superpowers/specs/2026-11-19-live-eco-preset-design.md`

## Global Constraints

- Model ids only in `ai/registry.py::PRESET_MODELS`.
- User-facing strings in French; code and docs in English.
- Other presets (`fast`, `balanced`, `max_quality`, `custom`) behave exactly as before.
- No prompt change, so no `*_VERSION` bump.

## Review Focus

- A live project created before the migration keeps its `balanced` preset (settings are copied at creation): its rerun still costs the old price. Expected, documented, not tested.
- `describe_visual` must not be called with an empty list (an OpenRouter analyst would still resolve models): pinned by the call-count test.
- The critique tests on live jobs now need `preset="balanced"` explicitly; they must keep testing the critique, not silently pass because it is off.

---

### Task 1: eco preset in the pipeline

**Files:**
- Modify: `apps/api/clipfactory/domain.py` (`Preset`, `live_project_settings`)
- Modify: `apps/api/clipfactory/ai/registry.py` (`AnalysisBudget`, `BUDGETS`, `PRESET_MODELS`)
- Modify: `apps/api/clipfactory/pipeline/stages_editorial.py` (`stage_analyze`, `stage_rank`)
- Create: `apps/api/migrations/versions/c4a7e9d1b2f3_live_channels_eco_preset.py`
- Test: `apps/api/tests/test_twitch_live.py`

**Interfaces:**
- Produces: `Preset` includes `"eco"`; `AnalysisBudget.transcript_moments: bool = True`, `AnalysisBudget.editorial_ranking: bool = True`;
  `BUDGETS["eco"] = AnalysisBudget(2.5, 0.0, 0, 0, 512, critique=False, video_input=False, transcript_moments=False, editorial_ranking=False)`;
  `PRESET_MODELS["eco"]` text tasks `["anthropic/claude-haiku-5.5", "google/gemini-3.1-flash-lite"]`, vision tasks `["google/gemini-3.1-flash-lite"]`.

- [ ] **Step 1: failing test** `test_a_live_moment_costs_one_ai_call` (slow): wrap every `FakeAnalyst` method to record its name, run `_live_job`; assert the recorded calls are exactly `["social"]` and the clip's `score_basis == "heuristic"`.
- [ ] **Step 2:** run `uv run pytest tests/test_twitch_live.py -k one_ai_call` → FAIL (find_moments, describe_visual, score, critique recorded).
- [ ] **Step 3:** implement the budget flags, the `eco` entries, `live_project_settings(preset="eco")`, the stage guards (`budget.transcript_moments`, `top_k > 0`, `budget.editorial_ranking`), and one French log line each in analyze and rank saying the preset skipped them.
- [ ] **Step 4:** pass `preset="balanced"` to `_live_job` in the three final-check tests.
- [ ] **Step 5:** migration: `live_channels.settings.preset` `balanced` → `eco` (downgrade: `eco` → `balanced`), via `json_set`.
- [ ] **Step 6:** `uv run pytest -m "not live"` → all pass.

### Task 2: dashboard

**Files:** `apps/web/src/lib/types.ts`, `apps/web/src/lib/format.ts`, `apps/web/src/app/parametres/page.tsx`, `apps/web/src/app/nouveau/page.tsx`

- [ ] **Step 1:** add `"eco"` to `Preset`, both zod enums, and `PRESET_LABELS.eco = "Économique"`.
- [ ] **Step 2:** `scripts/test.sh` → passes (tsc, eslint, vitest included).

### Task 3: apply and document

- [ ] **Step 1:** `uv run alembic upgrade head` on the real DB; the 17 channels read `eco`.
- [ ] **Step 2:** CLAUDE.md (live rule), ARCHITECTURE.md (presets), README.md (presets + live cost).
- [ ] **Step 3 (needs credit):** one real live job → `inference_usage` has a single `social_caption` row.
