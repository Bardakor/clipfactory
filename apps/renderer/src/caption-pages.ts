import { createTikTokStyleCaptions, type Caption } from "@remotion/captions";

export type PageToken = { text: string; fromMs: number; toMs: number };

export type CaptionPage = {
  startMs: number;
  endMs: number;
  tokens: PageToken[];
  /** False when any word on the page has no measured timing: such pages never get a word highlight. */
  measured: boolean;
};

/** How long a page stays on screen after its last word, unless the next page starts sooner. */
export const HOLD_MS = 350;

/**
 * Group caption tokens into on-screen pages with Remotion's caption tooling.
 * Page boundaries are decided upstream (`pageBreakAfter`), so the time-based
 * combining window is effectively disabled here.
 */
export const buildPages = (captions: Caption[]): CaptionPage[] => {
  if (captions.length === 0) {
    return [];
  }
  const unmeasured = new Set(captions.filter((c) => c.timestampMs === null).map((c) => c.startMs));
  const { pages } = createTikTokStyleCaptions({
    captions,
    combineTokensWithinMilliseconds: Number.MAX_SAFE_INTEGER,
  });
  return pages
    .filter((page) => page.tokens.length > 0)
    .map((page) => {
      const lastWordEnd = Math.max(...page.tokens.map((t) => t.toMs));
      const nextStart = page.startMs + page.durationMs;
      return {
        startMs: page.startMs,
        endMs: Math.max(page.startMs + 1, Math.min(nextStart, lastWordEnd + HOLD_MS)),
        tokens: page.tokens.map((t) => ({ text: t.text, fromMs: t.fromMs, toMs: t.toMs })),
        measured: !page.tokens.some((t) => unmeasured.has(t.fromMs)),
      };
    });
};

export const pageAt = (pages: CaptionPage[], ms: number): CaptionPage | null =>
  pages.find((p) => ms >= p.startMs && ms < p.endMs) ?? null;
