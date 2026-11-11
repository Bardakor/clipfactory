import { create } from "zustand";
import type { ClipDetail, FacecamPosition, Layout, SubtitleOptions, SubtitleStyle } from "./types";

type Crop = { x: number; y: number; w: number; h: number };

export type EditorDraft = {
  clipId: string | null;
  startMs: number;
  endMs: number;
  layout: Layout;
  crop: Crop;
  facecamPosition: FacecamPosition | null; // null = the project's choice
  style: SubtitleStyle;
  options: SubtitleOptions;
  captionEdits: Record<number, string>;
  title: string;
  caption: string;
  hashtags: string;
};

type EditorState = EditorDraft & {
  load: (clip: ClipDetail["clip"]) => void;
  set: (patch: Partial<EditorDraft>) => void;
  editCaption: (index: number, text: string) => void;
};

export const DEFAULT_OPTIONS: SubtitleOptions = { font: null, scale: 1, accent: null, position: "auto", emphasis: true };
const DEFAULT_CROP: Crop = { x: 0.22, y: 0, w: 0.56, h: 1 };

export function draftFromClip(clip: ClipDetail["clip"]): EditorDraft {
  return {
    clipId: clip.id,
    startMs: clip.start_ms,
    endMs: clip.end_ms,
    layout: clip.layout,
    crop: clip.layout_params?.manual ?? DEFAULT_CROP,
    facecamPosition: clip.layout_params?.facecam_position ?? null,
    style: clip.subtitle_style,
    options: { ...DEFAULT_OPTIONS, ...(clip.subtitle_options ?? {}) },
    captionEdits: {},
    title: clip.title,
    caption: clip.social?.caption ?? "",
    hashtags: (clip.social?.hashtags ?? []).join(" "),
  };
}

/** Local, unsaved editor state. The server stays the source of truth; this is only what the user is changing. */
export const useEditor = create<EditorState>((set) => ({
  ...draftFromClip({ id: "", start_ms: 0, end_ms: 0, layout: "auto", subtitle_style: "viral", title: "" } as ClipDetail["clip"]),
  clipId: null,
  load: (clip) => set(draftFromClip(clip)),
  set: (patch) => set(patch),
  editCaption: (index, text) => set((state) => ({ captionEdits: { ...state.captionEdits, [index]: text } })),
}));

/** The PATCH body containing only what differs from the saved clip. Empty object = nothing to save. */
export function buildPatch(draft: EditorDraft, clip: ClipDetail["clip"]): Record<string, unknown> {
  const patch: Record<string, unknown> = {};
  const trimmed = draft.startMs !== clip.start_ms || draft.endMs !== clip.end_ms;
  if (trimmed) {
    patch.start_ms = draft.startMs;
    patch.end_ms = draft.endMs;
  }
  if (draft.layout !== clip.layout) patch.layout = draft.layout;
  if (draft.layout === "manual" && JSON.stringify(draft.crop) !== JSON.stringify(clip.layout_params?.manual)) patch.manual_crop = draft.crop;
  if (draft.facecamPosition && draft.facecamPosition !== clip.layout_params?.facecam_position) patch.facecam_position = draft.facecamPosition;
  if (draft.style !== clip.subtitle_style) patch.subtitle_style = draft.style;
  const savedOptions = { ...DEFAULT_OPTIONS, ...(clip.subtitle_options ?? {}) };
  if (JSON.stringify(draft.options) !== JSON.stringify(savedOptions)) patch.subtitle_options = draft.options;
  // Caption pages are rebuilt server-side when the trim changes, so page edits only apply to an unchanged trim.
  if (!trimmed) {
    const edits = Object.fromEntries(Object.entries(draft.captionEdits).filter(([index, text]) => clip.caption_pages[Number(index)]?.text !== text.trim()));
    if (Object.keys(edits).length > 0) patch.caption_edits = edits;
  }
  if (draft.title !== clip.title) patch.title = draft.title;
  if (draft.caption !== (clip.social?.caption ?? "")) patch.social_caption = draft.caption;
  const tags = draft.hashtags.split(/\s+/).filter(Boolean);
  if (tags.join(" ") !== (clip.social?.hashtags ?? []).join(" ")) patch.hashtags = tags;
  return patch;
}
