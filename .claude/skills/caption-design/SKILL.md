---
name: caption-design
description: Use when changing CLIPFACTORY subtitles - caption tokens and word timing, page breaking, subtitle text editing, SRT export, caption styles and typography, safe zones, or the Remotion caption components.
---

# Caption design

Pipeline: transcript words → `render/captions.py::build_captions` (clip-relative `Caption` tokens with page
breaks) → `apps/renderer/src/caption-pages.ts` (`createTikTokStyleCaptions`) → `Captions.tsx` (styles).
Load the installed `remotion-captions` skill before touching the renderer side.

## Timing integrity

- A token carries `timestampMs` **only if its timing was measured** (Whisper word timestamps). Segment-level
  transcripts, and lines whose word count the user changed, get `timestampMs: null`.
- A page with any unmeasured token is never highlighted word by word. Do not interpolate word times to make the
  highlight "work" — that is fabricated alignment.
- Whisper splits elisions (`c` + `'est`, `l` + `'agent`). A piece with no leading space is merged into the
  previous token and extends its end time.
- Token text keeps a leading space; the renderer relies on it (whitespace-sensitive).

## Page breaking (Python, not React)

`_paginate` sets `pageBreakAfter` when the next word would overflow the style's page length (`PAGE_CHARS`), after
a pause longer than 650 ms, or at a sentence end once the page is half full. The renderer disables time-based
combining, so pages are exactly what Python decided — and exactly what the editor shows.

A page disappears 350 ms after its last word (`HOLD_MS`) unless the next page starts sooner: text must not linger
over silence.

## Styles

| Style | Font | Look | Spoken word |
| --- | --- | --- | --- |
| `viral` | Montserrat 900, caps | white, heavy black outline | turns the accent colour |
| `clean` | Inter 600 | white, soft shadow | upcoming words dimmed |
| `gaming` | Bebas Neue, caps | white, outline, slight skew | dark text on an accent box |

Rules that apply to any new style:

- **Readability beats motion.** One short fade-and-rise per page. No per-word bounce or scale; nothing that
  changes the width of a line while it is on screen (it makes neighbours jump).
- Sizes are fractions of the output width so the half-scale preview matches the final render.
- Outline via `-webkit-text-stroke` + `paint-order: stroke fill`; high contrast on any gameplay.
- Every word has the same padding in `gaming`, so the highlight box never shifts layout.
- Fonts are local files in `apps/renderer/public/fonts` loaded with `@remotion/fonts` (which holds the render
  until ready). Adding a font: add it to `scripts/copy-fonts.mjs`, `src/fonts.ts`, `SubtitleOptions.font` and the
  editor's font list. Check it covers French (latin + latin-ext).

## Placement

`layout.caption_y` puts captions just below the gameplay panel, inside the platform-safe band (top 11 %, bottom
22 %, sides 7 % reserved for TikTok/Reels UI) — so they do not cover the game HUD. Explicit positions
(`top/middle/bottom`) stay in the same band.

## Editing and export

- The editor edits **pages**. `apply_page_edits`: same word count → timings kept word for word; different count →
  words share the page's span and lose measured timing; empty → page removed.
- Editing the *transcript* rebuilds captions of affected clips (unless hand-edited) and marks exports stale.
- `to_srt` merges pages into reading cues (≤42 characters, ≤4.5 s). The `.srt` and a JSON transcript are exported
  with every clip, burned-in or not (`burn_subtitles`).

## Verify

```bash
cd apps/api && uv run pytest tests/test_units.py -k "caption or page or srt or elided or precision"
cd apps/renderer && pnpm test
```
Then render a preview and read a frame at a moment where a word is being spoken: is the right word highlighted,
is the line inside the safe band, is every character present (é, ç, œ, apostrophes)?
