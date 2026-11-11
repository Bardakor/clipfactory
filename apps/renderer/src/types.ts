import type { Caption } from "@remotion/captions";

/** A rectangle in normalised source coordinates (0–1). */
export type Rect = { x: number; y: number; w: number; h: number };

/** Draw `src` (part of the source frame) into an output-pixel rectangle. */
export type Panel = { src: Rect; x: number; y: number; w: number; h: number; radius: number };

export type LayoutSegment = { from_ms: number; to_ms: number; panels: Panel[] };

export type Layout = {
  used: string;
  segments: LayoutSegment[];
  /** Centre line of the caption block, in output pixels. */
  caption_y: number;
  caption_max_width: number;
};

export type SubtitleStyle = "viral" | "clean" | "gaming";

export type SubtitleOptions = {
  font?: string | null;
  scale?: number;
  accent?: string | null;
  position?: "auto" | "top" | "middle" | "bottom";
  emphasis?: boolean;
};

/** Input props of the `Clip` composition — written by the Python renderer service. */
export type ClipProps = {
  src: string;
  background: string;
  width: number;
  height: number;
  fps: number;
  durationInFrames: number;
  hasAudio: boolean;
  layout: Layout;
  captions: Caption[];
  style: SubtitleStyle;
  options: SubtitleOptions;
};
