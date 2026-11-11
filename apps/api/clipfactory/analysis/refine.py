"""Deterministic clip-boundary refinement against speech, silence and scene cuts."""

from __future__ import annotations

from clipfactory.domain import Segment
from clipfactory.timeutil import clamp_ms

START_SEARCH_BACK_MS = 3500
START_SEARCH_FWD_MS = 2000
END_SEARCH_BACK_MS = 2000
END_SEARCH_FWD_MS = 4500
LEAD_IN_MS = 180  # breathing room before the first word
TAIL_MS = 350  # let the last syllable and the start of the reaction ring out
CUT_SNAP_MS = 500
REACTION_GAP_MS = 1200  # speech resuming this soon after the end is still part of the moment


def _word_spans(segments: list[Segment]) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for seg in segments:
        if seg.words:
            spans.extend((w.s, w.e) for w in seg.words)
        else:
            spans.append((seg.start_ms, seg.end_ms))
    return spans


def _outside_words(ms: int, spans: list[tuple[int, int]], *, towards: int) -> int:
    """Move ``ms`` out of any word it falls inside (``towards`` = -1 earlier, +1 later)."""
    for s, e in spans:
        if s < ms < e:
            return s if towards < 0 else e
    return ms


def refine_boundaries(
    start_ms: int,
    end_ms: int,
    *,
    segments: list[Segment],
    silences: list[tuple[int, int]],
    cuts_ms: list[int],
    duration_ms: int,
    min_len_ms: int,
    max_len_ms: int,
) -> tuple[int, int, list[str]]:
    """Return (start, end, notes). Never starts or ends inside a word when timing allows."""
    notes: list[str] = []
    spans = _word_spans(segments)

    # ── start: the beginning of a sentence, ideally one that follows a pause ──
    starts = [s for s in segments if start_ms - START_SEARCH_BACK_MS <= s.start_ms <= start_ms + START_SEARCH_FWD_MS]
    if starts:
        def start_cost(seg: Segment) -> float:
            after_pause = any(a <= seg.start_ms <= b + 250 for a, b in silences) or not any(
                o.end_ms > seg.start_ms - 300 and o.end_ms <= seg.start_ms for o in segments if o is not seg
            )
            return abs(seg.start_ms - start_ms) - (1200 if after_pause else 0)

        best = min(starts, key=start_cost)
        new_start = best.start_ms - LEAD_IN_MS
        if new_start != start_ms:
            notes.append("début calé sur un début de phrase")
        start_ms = new_start
    start_ms = _outside_words(start_ms, spans, towards=-1)

    # ── end: the end of a sentence, extended through an immediate reaction ──
    ends = [s for s in segments if end_ms - END_SEARCH_BACK_MS <= s.end_ms <= end_ms + END_SEARCH_FWD_MS]
    if ends:
        best_end = min(ends, key=lambda s: abs(s.end_ms - end_ms)).end_ms
        extended = True
        while extended:
            extended = False
            for seg in segments:
                if best_end < seg.start_ms <= best_end + REACTION_GAP_MS and seg.end_ms <= end_ms + END_SEARCH_FWD_MS:
                    best_end = seg.end_ms
                    extended = True
                    notes.append("fin prolongée jusqu'à la réaction")
                    break
        if best_end != end_ms and "fin prolongée jusqu'à la réaction" not in notes:
            notes.append("fin calée sur une fin de phrase")
        end_ms = best_end + TAIL_MS
    end_ms = _outside_words(end_ms, spans, towards=+1)

    # A hard visual cut right after the end makes a cleaner out-point than mid-shot.
    near_cut = [c for c in cuts_ms if end_ms - CUT_SNAP_MS <= c <= end_ms + CUT_SNAP_MS]
    if near_cut:
        cut = min(near_cut, key=lambda c: abs(c - end_ms))
        if _outside_words(cut, spans, towards=+1) == cut:
            end_ms = cut
            notes.append("fin calée sur un changement de plan")

    # ── length limits ──
    start_ms = clamp_ms(start_ms, 0, duration_ms)
    end_ms = clamp_ms(end_ms, 0, duration_ms)
    if end_ms - start_ms > max_len_ms:
        # Keep the ending (payoff + reaction); trim the setup to the nearest later sentence start.
        target = end_ms - max_len_ms
        later = [s.start_ms - LEAD_IN_MS for s in segments if target <= s.start_ms - LEAD_IN_MS < end_ms - min_len_ms]
        start_ms = max(0, min(later)) if later else _outside_words(target, spans, towards=+1)
        notes.append("mise en place raccourcie pour respecter la durée maximale")
    if end_ms - start_ms < min_len_ms:
        missing = min_len_ms - (end_ms - start_ms)
        new_end = min(duration_ms, end_ms + missing)
        new_start = max(0, start_ms - (missing - (new_end - end_ms)))
        start_ms = _outside_words(new_start, spans, towards=-1)
        end_ms = _outside_words(new_end, spans, towards=+1)
        notes.append("allongé pour atteindre la durée minimale")
    return clamp_ms(start_ms, 0, duration_ms), clamp_ms(end_ms, 0, duration_ms), notes
