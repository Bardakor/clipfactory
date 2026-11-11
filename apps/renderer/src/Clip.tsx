import { Audio, Video } from "@remotion/media";
import type React from "react";
import { AbsoluteFill, Sequence, useVideoConfig } from "remotion";
import { Captions } from "./Captions";
import "./fonts";
import type { ClipProps, Panel } from "./types";

const PanelView: React.FC<{ panel: Panel; src: string; trimBefore: number }> = ({ panel, src, trimBefore }) => {
  // The panel is a window onto the source: the video is scaled so that `panel.src` exactly fills it.
  const videoW = panel.w / panel.src.w;
  const videoH = panel.h / panel.src.h;
  return (
    <div
      style={{
        position: "absolute",
        left: panel.x,
        top: panel.y,
        width: panel.w,
        height: panel.h,
        overflow: "hidden",
        borderRadius: panel.radius,
        boxShadow: "0 18px 60px rgba(0,0,0,0.5)",
      }}
    >
      <Video
        src={src}
        muted
        trimBefore={trimBefore}
        objectFit="fill"
        style={{ position: "absolute", width: videoW, height: videoH, left: -panel.src.x * videoW, top: -panel.src.y * videoH }}
      />
    </div>
  );
};

export const Clip: React.FC<ClipProps> = ({ src, background, hasAudio, layout, captions, style, options }) => {
  const { fps, durationInFrames } = useVideoConfig();
  const toFrame = (ms: number) => Math.round((ms / 1000) * fps);
  return (
    <AbsoluteFill style={{ backgroundColor: "#0B0B0F" }}>
      {/* Backdrop: a small pre-blurred copy of the same footage, scaled up. */}
      <Video src={background} muted objectFit="cover" style={{ width: "100%", height: "100%" }} />
      <AbsoluteFill style={{ backgroundColor: "rgba(6,6,10,0.28)" }} />

      {layout.segments.map((segment) => {
        const from = toFrame(segment.from_ms);
        const length = Math.max(1, Math.min(durationInFrames, toFrame(segment.to_ms)) - from);
        return (
          <Sequence key={segment.from_ms} from={from} durationInFrames={length} layout="none">
            {segment.panels.map((panel, i) => (
              // trimBefore keeps every panel on the clip's own timeline, whatever segment it lives in.
              <PanelView key={i} panel={panel} src={src} trimBefore={from} />
            ))}
          </Sequence>
        );
      })}

      {/* One continuous audio track, independent of framing changes, so sound never jumps at a cut. */}
      {hasAudio ? <Audio src={src} /> : null}

      <Captions captions={captions} style={style} options={options} y={layout.caption_y} maxWidth={layout.caption_max_width} />
    </AbsoluteFill>
  );
};
