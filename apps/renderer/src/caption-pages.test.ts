import type { Caption } from "@remotion/captions";
import { describe, expect, it } from "vitest";
import { buildPages, HOLD_MS, pageAt } from "./caption-pages";

const word = (text: string, startMs: number, endMs: number, extra: Partial<Caption> = {}): Caption => ({
  text: ` ${text}`,
  startMs,
  endMs,
  timestampMs: Math.round((startMs + endMs) / 2),
  confidence: 0.9,
  ...extra,
});

describe("caption pages", () => {
  it("splits on pageBreakAfter and keeps word timings", () => {
    const pages = buildPages([
      word("attends", 0, 300),
      word("attends", 320, 600, { pageBreakAfter: true }),
      word("freine", 2000, 2400, { pageBreakAfter: true }),
    ]);
    expect(pages).toHaveLength(2);
    expect(pages[0]!.tokens.map((t) => t.text.trim())).toEqual(["attends", "attends"]);
    expect(pages[0]!.tokens[1]).toMatchObject({ fromMs: 320, toMs: 600 });
    expect(pages[0]!.measured).toBe(true);
  });

  it("does not leave a page on screen through a long silence", () => {
    const pages = buildPages([word("un", 0, 400, { pageBreakAfter: true }), word("deux", 5000, 5400, { pageBreakAfter: true })]);
    expect(pages[0]!.endMs).toBe(400 + HOLD_MS);
    expect(pageAt(pages, 2000)).toBeNull();
    expect(pageAt(pages, 5100)).toBe(pages[1]);
  });

  it("hands over to the next page without overlap when speech is continuous", () => {
    const pages = buildPages([word("un", 0, 400, { pageBreakAfter: true }), word("deux", 450, 800, { pageBreakAfter: true })]);
    expect(pages[0]!.endMs).toBe(450);
  });

  it("marks pages without measured word timing so they are never highlighted word by word", () => {
    const pages = buildPages([{ text: " une phrase entière", startMs: 0, endMs: 1500, timestampMs: null, confidence: null, pageBreakAfter: true }]);
    expect(pages[0]!.measured).toBe(false);
  });

  it("returns nothing for an empty transcript", () => {
    expect(buildPages([])).toEqual([]);
  });
});
