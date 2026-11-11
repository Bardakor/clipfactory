"""Caption tokens (clip-relative, @remotion/captions' ``Caption`` shape), paging, editing and SRT."""

from __future__ import annotations

from dataclasses import dataclass

from clipfactory.domain import Caption, Segment
from clipfactory.timeutil import clamp_ms, srt_ts

# Characters per on-screen page, per style: big bold styles show fewer words at once.
PAGE_CHARS = {"viral": 16, "gaming": 20, "clean": 30}
PAUSE_BREAK_MS = 650
_SENTENCE_END = (".", "!", "?", "…")
SRT_CUE_CHARS = 42
SRT_CUE_MS = 4500


def build_captions(segments: list[Segment], start_ms: int, end_ms: int, style: str) -> tuple[list[Caption], str]:
    """Tokens for the words spoken inside [start, end), with page breaks chosen for readability.

    Returns (captions, precision). With word-level timing each token carries a
    measured ``timestampMs``. Without it, a segment's text is split into short
    groups timed proportionally to their length and ``timestampMs`` stays null,
    which tells the renderer not to highlight individual words it has no timing for.
    """
    duration = end_ms - start_ms
    max_chars = PAGE_CHARS.get(style, 20)
    tokens: list[Caption] = []
    measured_all = True
    for seg in segments:
        if seg.end_ms <= start_ms or seg.start_ms >= end_ms:
            continue
        if seg.words:
            for w in seg.words:
                mid = (w.s + w.e) // 2
                if not (start_ms <= mid < end_ms) or not w.w.strip():
                    continue
                s, e = clamp_ms(w.s - start_ms, 0, duration), clamp_ms(w.e - start_ms, 0, duration)
                tokens.append(Caption(text=" " + w.w.strip(), startMs=s, endMs=max(e, s + 1), timestampMs=(s + e) // 2, confidence=w.p))
        else:
            measured_all = False
            tokens.extend(_split_untimed(seg, start_ms, end_ms, max_chars))
    tokens.sort(key=lambda c: c.startMs)
    return _paginate(tokens, max_chars), "word" if measured_all and tokens else "segment"


def _split_untimed(seg: Segment, start_ms: int, end_ms: int, max_chars: int) -> list[Caption]:
    groups: list[str] = []
    for word in seg.text.split():
        if groups and len(groups[-1]) + 1 + len(word) <= max_chars:
            groups[-1] += " " + word
        else:
            groups.append(word)
    total = sum(len(g) for g in groups) or 1
    span = seg.end_ms - seg.start_ms
    out: list[Caption] = []
    cursor = seg.start_ms
    duration = end_ms - start_ms
    for group in groups:
        length = span * len(group) // total
        s, e = cursor, cursor + length
        cursor = e
        if e <= start_ms or s >= end_ms:
            continue
        out.append(Caption(text=" " + group, startMs=clamp_ms(s - start_ms, 0, duration),
                           endMs=clamp_ms(e - start_ms, 1, duration), timestampMs=None, pageBreakAfter=True))  # fmt: skip
    return out


def _paginate(tokens: list[Caption], max_chars: int) -> list[Caption]:
    """Set ``pageBreakAfter`` so pages are short, end on pauses and never split mid-phrase needlessly."""
    chars = 0
    for i, token in enumerate(tokens):
        if token.timestampMs is None:  # untimed groups are already one page each
            chars = 0
            continue
        chars += len(token.text.strip()) + (1 if chars else 0)
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None
        ends_sentence = token.text.rstrip().endswith(_SENTENCE_END)
        long_pause = nxt is not None and nxt.startMs - token.endMs > PAUSE_BREAK_MS
        would_overflow = nxt is not None and chars + 1 + len(nxt.text.strip()) > max_chars
        if nxt is None or long_pause or would_overflow or (ends_sentence and chars >= max_chars // 2):
            token.pageBreakAfter = True
            chars = 0
        else:
            token.pageBreakAfter = None
    return tokens


@dataclass
class Page:
    index: int
    first: int  # token index range [first, last]
    last: int
    text: str
    start_ms: int
    end_ms: int
    measured: bool


def pages_of(captions: list[Caption]) -> list[Page]:
    pages: list[Page] = []
    first = 0
    for i, token in enumerate(captions):
        if token.pageBreakAfter or i == len(captions) - 1:
            group = captions[first : i + 1]
            pages.append(Page(len(pages), first, i, "".join(t.text for t in group).strip(), group[0].startMs,
                              group[-1].endMs, all(t.timestampMs is not None for t in group)))  # fmt: skip
            first = i + 1
    return pages


def apply_page_edits(captions: list[Caption], edits: dict[int, str]) -> list[Caption]:
    """Replace the text of whole pages. Timing is preserved word-for-word when the word count is
    unchanged; otherwise the new words share the page's time span and lose word-level highlighting."""
    out: list[Caption] = []
    for page in pages_of(captions):
        group = captions[page.first : page.last + 1]
        if page.index not in edits or edits[page.index].strip() == page.text:
            out.extend(group)
            continue
        words = edits[page.index].split()
        if not words:  # an emptied page is removed from the video
            continue
        if len(words) == len(group) and page.measured:
            new = [g.model_copy(update={"text": " " + w}) for g, w in zip(group, words, strict=True)]
        else:
            span = page.end_ms - page.start_ms
            new = [
                Caption(text=" " + w, startMs=page.start_ms + span * i // len(words),
                        endMs=page.start_ms + span * (i + 1) // len(words), timestampMs=None)  # fmt: skip
                for i, w in enumerate(words)
            ]
        for token in new:
            token.pageBreakAfter = None
        new[-1].pageBreakAfter = True
        out.extend(new)
    return out


def to_srt(captions: list[Caption]) -> str:
    """Sidecar subtitles: pages merged into comfortable reading cues."""
    cues: list[tuple[int, int, str]] = []
    for page in pages_of(captions):
        if cues:
            s, e, text = cues[-1]
            joined = f"{text} {page.text}"
            if len(joined) <= SRT_CUE_CHARS and page.end_ms - s <= SRT_CUE_MS and page.start_ms - e <= PAUSE_BREAK_MS \
                    and not text.endswith(_SENTENCE_END):  # fmt: skip
                cues[-1] = (s, page.end_ms, joined)
                continue
        cues.append((page.start_ms, page.end_ms, page.text))
    blocks = [f"{i}\n{srt_ts(s)} --> {srt_ts(max(e, s + 1))}\n{text}\n" for i, (s, e, text) in enumerate(cues, start=1)]
    return "\n".join(blocks)
