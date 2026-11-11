import type React from "react";
import { Composition } from "remotion";
import { Clip } from "./Clip";
import type { ClipProps } from "./types";

const defaults: ClipProps = {
  src: "",
  background: "",
  width: 1080,
  height: 1920,
  fps: 30,
  durationInFrames: 30,
  hasAudio: false,
  layout: { used: "full", segments: [], caption_y: 1300, caption_max_width: 928 },
  captions: [],
  style: "viral",
  options: {},
};

export const Root: React.FC = () => (
  <Composition
    id="Clip"
    component={Clip}
    defaultProps={defaults}
    width={defaults.width}
    height={defaults.height}
    fps={defaults.fps}
    durationInFrames={defaults.durationInFrames}
    // Size, frame rate and length all come from the clip being rendered.
    calculateMetadata={({ props }) => ({
      width: props.width,
      height: props.height,
      fps: props.fps,
      durationInFrames: props.durationInFrames,
    })}
  />
);
