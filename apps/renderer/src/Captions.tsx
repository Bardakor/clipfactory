import type { Caption } from "@remotion/captions";
import type React from "react";
import { useMemo } from "react";
import { Easing, interpolate, useCurrentFrame, useVideoConfig } from "remotion";
import { buildPages, pageAt, type CaptionPage } from "./caption-pages";
import type { SubtitleOptions, SubtitleStyle } from "./types";

type StyleSpec = {
  font: string;
  weight: number;
  /** Font size as a fraction of the output width, so previews at half scale look identical. */
  size: number;
  uppercase: boolean;
  accent: string;
  letterSpacing: string;
  lineHeight: number;
};

const STYLES: Record<SubtitleStyle, StyleSpec> = {
  viral: { font: "Montserrat", weight: 900, size: 0.078, uppercase: true, accent: "#FFE14D", letterSpacing: "-0.01em", lineHeight: 1.08 },
  clean: { font: "Inter", weight: 600, size: 0.05, uppercase: false, accent: "#FFFFFF", letterSpacing: "-0.005em", lineHeight: 1.25 },
  gaming: { font: "Bebas Neue", weight: 400, size: 0.094, uppercase: true, accent: "#4DFFA0", letterSpacing: "0.02em", lineHeight: 1.05 },
};

const outline = (px: number): React.CSSProperties => ({
  WebkitTextStroke: `${px}px #000`,
  paintOrder: "stroke fill",
  textShadow: `0 ${px * 0.35}px ${px * 0.9}px rgba(0,0,0,0.55)`,
});

const Word: React.FC<{
  text: string;
  state: "past" | "active" | "upcoming" | "plain";
  style: SubtitleStyle;
  accent: string;
  fontSize: number;
}> = ({ text, state, style, accent, fontSize }) => {
  const base: React.CSSProperties = { display: "inline-block", color: "#fff" };
  if (style === "viral") {
    // The spoken word turns to the accent colour. No bounce: size changes on every word hurt legibility.
    return <span style={{ ...base, ...outline(fontSize * 0.16), color: state === "active" ? accent : "#fff" }}>{text}</span>;
  }
  if (style === "gaming") {
    const active = state === "active";
    return (
      <span
        style={{
          ...base,
          ...(active ? {} : outline(fontSize * 0.12)),
          color: active ? "#0B0B0F" : "#fff",
          backgroundColor: active ? accent : "transparent",
          // Every word carries the same padding, so the highlight box never shifts its neighbours.
          borderRadius: fontSize * 0.12,
          padding: `0 ${fontSize * 0.1}px`,
          transform: "skewX(-5deg)",
        }}
      >
        {text}
      </span>
    );
  }
  // clean: words already spoken are solid white, upcoming ones slightly dimmed.
  return (
    <span
      style={{
        ...base,
        color: accent,
        opacity: state === "upcoming" ? 0.62 : 1,
        textShadow: "0 2px 4px rgba(0,0,0,0.9), 0 0 18px rgba(0,0,0,0.65)",
      }}
    >
      {text}
    </span>
  );
};

const PageView: React.FC<{
  page: CaptionPage;
  ms: number;
  style: SubtitleStyle;
  spec: StyleSpec;
  accent: string;
  fontSize: number;
  emphasis: boolean;
}> = ({ page, ms, style, spec, accent, fontSize, emphasis }) => {
  const { fps } = useVideoConfig();
  const sinceStart = ((ms - page.startMs) / 1000) * fps;
  // A short, quiet entrance: three frames of fade and a few pixels of rise.
  const enter = interpolate(sinceStart, [0, 3], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp", easing: Easing.out(Easing.cubic) });
  const highlight = emphasis && page.measured;
  return (
    <div
      style={{
        fontFamily: `"${spec.font}", sans-serif`,
        fontWeight: spec.weight,
        fontSize,
        lineHeight: spec.lineHeight,
        letterSpacing: spec.letterSpacing,
        textTransform: spec.uppercase ? "uppercase" : "none",
        textAlign: "center",
        textWrap: "balance",
        opacity: enter,
        transform: `translateY(${(1 - enter) * fontSize * 0.12}px)`,
      }}
    >
      {page.tokens.map((token, i) => {
        const state = !highlight ? "plain" : ms >= token.toMs ? "past" : ms >= token.fromMs ? "active" : "upcoming";
        // A word stays "active" through the gap until the next one starts, so the highlight never flickers off.
        const next = page.tokens[i + 1];
        const held = highlight && state === "past" && (next ? ms < next.fromMs : true) ? "active" : state;
        return (
          <span key={`${token.fromMs}-${i}`}>
            {i > 0 ? " " : null}
            <Word text={token.text.trim()} state={held} style={style} accent={accent} fontSize={fontSize} />
          </span>
        );
      })}
    </div>
  );
};

export const Captions: React.FC<{
  captions: Caption[];
  style: SubtitleStyle;
  options: SubtitleOptions;
  y: number;
  maxWidth: number;
}> = ({ captions, style, options, y, maxWidth }) => {
  const frame = useCurrentFrame();
  const { fps, width, height } = useVideoConfig();
  const pages = useMemo(() => buildPages(captions), [captions]);
  const ms = (frame / fps) * 1000;
  const page = pageAt(pages, ms);
  if (!page) {
    return null;
  }
  const base = STYLES[style] ?? STYLES.viral;
  const spec = { ...base, font: options.font ?? base.font };
  const fontSize = width * spec.size * (options.scale ?? 1);
  // Explicit positions stay inside the platform-safe band (clear of the top bar and the bottom caption/buttons).
  const centre = { auto: y, top: height * 0.17, middle: height * 0.5, bottom: height * 0.73 }[options.position ?? "auto"];
  return (
    <div style={{ position: "absolute", left: (width - maxWidth) / 2, width: maxWidth, top: centre, transform: "translateY(-50%)" }}>
      <PageView page={page} ms={ms} style={style} spec={spec} accent={options.accent ?? base.accent} fontSize={fontSize} emphasis={options.emphasis ?? true} />
    </div>
  );
};
