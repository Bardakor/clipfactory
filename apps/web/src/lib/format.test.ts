import { describe, expect, it } from "vitest";
import { buildPatch, draftFromClip } from "./editor-store";
import { bytes, chatBuckets, clamp, clock, elapsedMs, momentStatus, readyToPost, seconds, timecode, usd } from "./format";
import type { ClipDetail, LiveMoment } from "./types";

describe("formatting", () => {
  it("formats source timestamps", () => {
    expect(clock(0)).toBe("0:00");
    expect(clock(59_999)).toBe("0:59");
    expect(clock(3_661_000)).toBe("1:01:01");
    expect(clock(null)).toBe("—");
    expect(timecode(12_740)).toBe("0:12.7");
  });

  it("formats durations, sizes and money in French", () => {
    expect(seconds(850)).toBe("850 ms");
    expect(seconds(26_700)).toBe("26,7 s");
    expect(seconds(83_000)).toBe("1 min 23");
    expect(seconds(3_780_000)).toBe("1 h 03");
    expect(bytes(0)).toBe("0 o");
    expect(bytes(11_167_200)).toBe("10,6 Mo");
    expect(usd(0)).toBe("0,00 $");
    expect(usd(0.0012)).toBe("0,0012 $");
    expect(usd(1.5)).toBe("1,50 $");
  });

  it("measures elapsed time from real timestamps only", () => {
    expect(elapsedMs(null, null)).toBeNull();
    expect(elapsedMs("2026-01-01T00:00:00Z", "2026-01-01T00:00:05Z")).toBe(5000);
    expect(elapsedMs("2026-01-01T00:00:00Z", null, Date.parse("2026-01-01T00:00:02Z"))).toBe(2000);
    expect(clamp(5, 0, 3)).toBe(3);
  });
});

const clip = {
  id: "c1",
  start_ms: 10_000,
  end_ms: 30_000,
  layout: "auto",
  layout_params: null,
  subtitle_style: "viral",
  subtitle_options: {},
  title: "Titre",
  social: { caption: "Légende", hashtags: ["#gta5"] },
  caption_pages: [
    { index: 0, text: "un deux", start_ms: 0, end_ms: 800, measured: true },
    { index: 1, text: "trois", start_ms: 900, end_ms: 1400, measured: true },
  ],
} as unknown as ClipDetail["clip"];

describe("editor patch", () => {
  it("sends nothing when nothing changed", () => {
    expect(buildPatch(draftFromClip(clip), clip)).toEqual({});
  });

  it("sends only what changed", () => {
    const draft = { ...draftFromClip(clip), style: "gaming" as const, captionEdits: { 0: "un deux", 1: "TROIS" }, hashtags: "#gta5 #rp" };
    expect(buildPatch(draft, clip)).toEqual({ subtitle_style: "gaming", caption_edits: { 1: "TROIS" }, hashtags: ["#gta5", "#rp"] });
  });

  it("drops page edits when the trim changed, because pages are rebuilt for the new range", () => {
    const draft = { ...draftFromClip(clip), startMs: 9_000, captionEdits: { 1: "TROIS" } };
    expect(buildPatch(draft, clip)).toEqual({ start_ms: 9_000, end_ms: 30_000 });
  });

  it("includes the crop rectangle only for the manual layout", () => {
    const draft = { ...draftFromClip(clip), crop: { x: 0.1, y: 0, w: 0.5, h: 1 } };
    expect(buildPatch(draft, clip)).toEqual({});
    expect(buildPatch({ ...draft, layout: "manual" }, clip)).toEqual({ layout: "manual", manual_crop: { x: 0.1, y: 0, w: 0.5, h: 1 } });
  });
});

describe("live", () => {
  const moment: LiveMoment = {
    id: "m1", channel_id: "c1", login: "marlon", peak_at: "2026-11-19T20:15:00Z", chatters: 60, baseline: 8, z: 18.4,
    status: "queued", detail: null, project_id: "p1", job_status: "running", current_stage: "transcribe", clip: null,
  }; // prettier-ignore

  it("lays chat buckets on the clock and marks the ones that became a moment", () => {
    const buckets = chatBuckets({ t0: 1000, chatters: [8, null, 60] }, [1020]);
    expect(buckets).toEqual([
      { at: 1000, chatters: 8, spike: false },
      { at: 1010, chatters: null, spike: false }, // chat was not being read: unknown, not zero
      { at: 1020, chatters: 60, spike: true },
    ]);
    expect(chatBuckets(null, [])).toEqual([]);
  });

  it("says where a detected moment stands", () => {
    expect(momentStatus({ ...moment, status: "pending", project_id: null, job_status: null, current_stage: null })).toEqual({
      label: "Enregistrement du moment",
      tone: "info",
    });
    expect(momentStatus(moment)).toEqual({ label: "Transcription", tone: "info" });
    expect(momentStatus({ ...moment, job_status: "queued", current_stage: null })).toEqual({ label: "En file", tone: "muted" });
    expect(momentStatus({ ...moment, job_status: "succeeded", current_stage: null })).toEqual({ label: "Aucun clip retenu", tone: "muted" });
    expect(momentStatus({ ...moment, job_status: "failed", current_stage: null })).toEqual({ label: "Échec du traitement", tone: "destructive" });
    expect(momentStatus({ ...moment, project_id: null, job_status: null, current_stage: null })).toEqual({ label: "Projet supprimé", tone: "muted" });
    expect(momentStatus({ ...moment, status: "failed", detail: "Aucune image" })).toEqual({ label: "Aucune image", tone: "destructive" });
  });

  it("offers a clip for posting only once its pipeline has finished", () => {
    const clip = { id: "c1", render_status: "rendering" } as LiveMoment["clip"];
    expect(readyToPost({ ...moment, clip })).toBe(false); // the clip row exists as soon as it is chosen, before it is rendered
    expect(momentStatus({ ...moment, clip, current_stage: "render" })).toEqual({ label: "Rendu", tone: "info" });
    expect(readyToPost({ ...moment, clip, job_status: "failed" })).toBe(false);
    expect(readyToPost({ ...moment, job_status: "succeeded" })).toBe(false); // finished, but nothing was kept
    expect(readyToPost({ ...moment, clip, job_status: "succeeded" })).toBe(true);
  });
});
