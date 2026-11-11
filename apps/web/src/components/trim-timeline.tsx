"use client";

import { useRef } from "react";
import { clamp, timecode } from "@/lib/format";
import { cn } from "@/lib/utils";

const MIN_MS = 2000;
const PAD_MS = 15_000;

/** Trim control on the source timeline: drag either edge, or nudge it by a tenth of a second. */
export function TrimTimeline({
  startMs,
  endMs,
  originalStartMs,
  originalEndMs,
  sourceDurationMs,
  onChange,
}: {
  startMs: number;
  endMs: number;
  originalStartMs: number;
  originalEndMs: number;
  sourceDurationMs: number;
  onChange: (startMs: number, endMs: number) => void;
}) {
  const track = useRef<HTMLDivElement>(null);
  // The visible window stays anchored on the clip as first generated, so handles do not slide while dragging.
  const viewStart = Math.max(0, Math.min(originalStartMs, startMs) - PAD_MS);
  const viewEnd = Math.min(sourceDurationMs, Math.max(originalEndMs, endMs) + PAD_MS);
  const span = Math.max(1, viewEnd - viewStart);
  const percent = (ms: number) => ((ms - viewStart) / span) * 100;

  function drag(edge: "start" | "end", event: React.PointerEvent) {
    event.preventDefault();
    const rect = track.current?.getBoundingClientRect();
    if (!rect) return;
    const move = (e: PointerEvent) => {
      const ms = Math.round((viewStart + clamp((e.clientX - rect.left) / rect.width, 0, 1) * span) / 100) * 100;
      if (edge === "start") onChange(clamp(ms, 0, endMs - MIN_MS), endMs);
      else onChange(startMs, clamp(ms, startMs + MIN_MS, sourceDurationMs));
    };
    const stop = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", stop);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", stop);
  }

  const nudge = (edge: "start" | "end", delta: number) => {
    if (edge === "start") onChange(clamp(startMs + delta, 0, endMs - MIN_MS), endMs);
    else onChange(startMs, clamp(endMs + delta, startMs + MIN_MS, sourceDurationMs));
  };

  const handle = (edge: "start" | "end") => (
    <div
      role="slider"
      tabIndex={0}
      aria-label={edge === "start" ? "Début du clip" : "Fin du clip"}
      aria-valuenow={edge === "start" ? startMs : endMs}
      onPointerDown={(e) => drag(edge, e)}
      onKeyDown={(e) => {
        if (e.key === "ArrowLeft") nudge(edge, e.shiftKey ? -1000 : -100);
        if (e.key === "ArrowRight") nudge(edge, e.shiftKey ? 1000 : 100);
      }}
      className={cn("absolute top-0 z-10 flex h-full w-3 cursor-ew-resize items-center justify-center rounded-sm bg-primary outline-none focus-visible:ring-2 focus-visible:ring-ring", edge === "start" ? "-translate-x-full" : "")}
      style={{ left: `${percent(edge === "start" ? startMs : endMs)}%` }}
    >
      <span className="h-4 w-px bg-primary-foreground/70" />
    </div>
  );

  const nudges = (edge: "start" | "end") => (
    <div className="flex items-center gap-1">
      {[-1000, -100, 100, 1000].map((delta) => (
        <button key={delta} type="button" onClick={() => nudge(edge, delta)} className="tnum h-6 rounded bg-raised px-1.5 font-mono text-[10px] text-muted-foreground hover:text-foreground">
          {delta > 0 ? "+" : "−"}{Math.abs(delta) / 1000}s
        </button>
      ))}
    </div>
  );

  return (
    <div>
      <div ref={track} className="relative h-12 touch-none rounded-md bg-background select-none">
        {/* where the clip was when it was generated */}
        <div className="absolute inset-y-0 border-x border-dashed border-muted-foreground/40" style={{ left: `${percent(originalStartMs)}%`, width: `${percent(originalEndMs) - percent(originalStartMs)}%` }} />
        <div className="absolute inset-y-0 bg-primary/20" style={{ left: `${percent(startMs)}%`, width: `${percent(endMs) - percent(startMs)}%` }} />
        {handle("start")}
        {handle("end")}
      </div>
      <div className="tnum mt-1 flex justify-between font-mono text-[10px] text-muted-foreground/70">
        <span>{timecode(viewStart)}</span>
        <span>temps de la vidéo source</span>
        <span>{timecode(viewEnd)}</span>
      </div>
      <div className="mt-4 grid grid-cols-3 items-end gap-4">
        <div>
          <div className="mb-1 text-[11px] text-muted-foreground">Début</div>
          <div className="tnum mb-1.5 font-mono text-sm">{timecode(startMs)}</div>
          {nudges("start")}
        </div>
        <div className="text-center">
          <div className="mb-1 text-[11px] text-muted-foreground">Durée</div>
          <div className="tnum font-mono text-sm">{((endMs - startMs) / 1000).toFixed(1).replace(".", ",")} s</div>
        </div>
        <div className="flex flex-col items-end">
          <div className="mb-1 text-[11px] text-muted-foreground">Fin</div>
          <div className="tnum mb-1.5 font-mono text-sm">{timecode(endMs)}</div>
          {nudges("end")}
        </div>
      </div>
    </div>
  );
}
