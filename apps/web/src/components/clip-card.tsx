"use client";

import { Check, Copy, Download, FileText, Loader2, Pencil, RefreshCw, X } from "lucide-react";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { API_URL, mediaUrl } from "@/lib/api";
import { APPROVAL_LABELS, LAYOUT_LABELS, REJECTION_LABELS, RENDER_STATUS_LABELS, STYLE_LABELS, clock, seconds } from "@/lib/format";
import { clipActions, useAction } from "@/lib/queries";
import type { Clip } from "@/lib/types";
import { cn } from "@/lib/utils";
import { Score, TestBadge } from "./bits";

export function postText(clip: Clip): string {
  const social = clip.social ?? {};
  return [social.title ?? clip.title, social.caption ?? "", (social.hashtags ?? []).join(" ")].filter(Boolean).join("\n\n");
}

function IconButton({
  label,
  onClick,
  href,
  disabled,
  children,
  tone,
}: {
  label: string;
  onClick?: () => void;
  href?: string;
  disabled?: boolean;
  children: React.ReactNode;
  tone?: "ok" | "danger";
}) {
  const className = cn(
    "grid size-8 place-items-center rounded-md text-muted-foreground transition-colors hover:bg-raised hover:text-foreground disabled:pointer-events-none disabled:opacity-35",
    tone === "ok" && "text-ok hover:text-ok",
    tone === "danger" && "text-destructive hover:text-destructive",
  );
  if (href && !disabled) {
    return (
      <a href={href} className={className} title={label} aria-label={label}>
        {children}
      </a>
    );
  }
  return (
    <button type="button" className={className} title={label} aria-label={label} onClick={onClick} disabled={disabled}>
      {children}
    </button>
  );
}

/** Reject with a reason: the reason is what makes the feedback usable later. */
export function RejectMenu({ onReject, children }: { onReject: (reason: string) => void; children: (open: () => void) => React.ReactNode }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (event: MouseEvent) => {
      if (!ref.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [open]);
  return (
    <div className="relative" ref={ref}>
      {children(() => setOpen((v) => !v))}
      {open ? (
        <div className="absolute right-0 bottom-full z-20 mb-1.5 w-48 rounded-lg bg-popover p-1 shadow-xl ring-1 ring-border">
          <div className="px-2.5 pt-1.5 pb-1 text-[10px] tracking-wide text-muted-foreground uppercase">Motif du rejet</div>
          {Object.entries(REJECTION_LABELS).map(([reason, label]) => (
            <button
              key={reason}
              type="button"
              className="block w-full rounded-md px-2.5 py-1.5 text-left text-[13px] hover:bg-raised"
              onClick={() => {
                setOpen(false);
                onReject(reason);
              }}
            >
              {label}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

export function ClipCard({ clip, showProject }: { clip: Clip; showProject?: boolean }) {
  const feedback = useAction((body: { decision: string; rejection_reason?: string }) => clipActions.feedback(clip.id, body));
  const render = useAction(() => clipActions.render(clip.id, "final"), { success: "Rendu ajouté à la file" });
  const busy = ["queued", "rendering", "validating"].includes(clip.render_status);
  const video = mediaUrl(clip.video_url);

  return (
    <article className={cn("group flex flex-col overflow-hidden rounded-xl bg-surface", clip.approval === "rejected" && "opacity-55")}>
      <div className="relative aspect-[9/16] bg-black">
        {video ? (
          // #t=0.5 makes the browser show a real frame as the poster without a separate thumbnail file.
          <video key={video} src={`${video}#t=0.5`} controls preload="metadata" playsInline className="size-full object-contain" />
        ) : (
          <div className="grid size-full place-items-center px-6 text-center text-xs text-muted-foreground">
            {busy ? (
              <span className="flex flex-col items-center gap-2">
                <Loader2 className="size-5 animate-spin text-info" />
                {RENDER_STATUS_LABELS[clip.render_status]}
              </span>
            ) : clip.render_status === "failed" ? (
              <span className="text-destructive">
                Rendu échoué
                <span className="mt-1 block text-[11px] text-muted-foreground">
                  {clip.validation?.error?.message ?? clip.validation?.failures?.join(", ") ?? "Voir le journal du job"}
                </span>
              </span>
            ) : (
              "Pas encore rendu"
            )}
          </div>
        )}
        <div className="pointer-events-none absolute inset-x-0 top-0 flex items-start justify-between p-2.5">
          <span className="tnum rounded bg-black/65 px-1.5 py-0.5 text-[11px] font-medium text-white backdrop-blur">
            #{clip.rank} · {seconds(clip.duration_ms)}
          </span>
          {clip.is_test ? <TestBadge /> : null}
        </div>
      </div>

      <div className="flex flex-1 flex-col gap-3 p-4">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            {showProject && clip.project_name ? <div className="mb-0.5 truncate text-[11px] text-muted-foreground">{clip.project_name}</div> : null}
            <Link href={`/clips/${clip.id}`} className="line-clamp-2 text-sm leading-snug font-medium hover:underline">
              {clip.title || clip.main_event || "Clip sans titre"}
            </Link>
          </div>
          <Score value={clip.score} basis={clip.score_basis} />
        </div>

        <p className="line-clamp-3 text-[13px] leading-relaxed text-muted-foreground">{clip.justification}</p>

        <div className="mt-auto flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
          <span className="tnum font-mono">
            {clock(clip.start_ms)} → {clock(clip.end_ms)}
          </span>
          <span>{STYLE_LABELS[clip.subtitle_style]}</span>
          <span title={clip.resolved_layout?.reason}>{LAYOUT_LABELS[clip.resolved_layout?.used ?? clip.layout] ?? clip.layout}</span>
        </div>

        <div className="flex items-center justify-between text-[11px]">
          <span
            className={cn(
              "font-medium",
              clip.render_status === "rendered"
                ? "text-ok"
                : clip.render_status === "failed"
                  ? "text-destructive"
                  : clip.render_status === "stale"
                    ? "text-warn"
                    : "text-muted-foreground",
            )}
          >
            {RENDER_STATUS_LABELS[clip.render_status]}
          </span>
          <span
            className={cn(
              "font-medium",
              clip.approval === "approved" ? "text-ok" : clip.approval === "rejected" ? "text-destructive" : "text-muted-foreground",
            )}
          >
            {APPROVAL_LABELS[clip.approval]}
          </span>
        </div>

        <div className="-mx-1.5 -mb-1.5 flex items-center justify-between">
          <div className="flex">
            <IconButton
              label="Approuver"
              tone={clip.approval === "approved" ? "ok" : undefined}
              onClick={() => feedback.mutate({ decision: clip.approval === "approved" ? "reset" : "accepted" })}
            >
              <Check className="size-4" />
            </IconButton>
            <RejectMenu onReject={(reason) => feedback.mutate({ decision: "rejected", rejection_reason: reason })}>
              {(open) => (
                <IconButton
                  label="Rejeter"
                  tone={clip.approval === "rejected" ? "danger" : undefined}
                  onClick={clip.approval === "rejected" ? () => feedback.mutate({ decision: "reset" }) : open}
                >
                  <X className="size-4" />
                </IconButton>
              )}
            </RejectMenu>
            <Link
              href={`/clips/${clip.id}`}
              title="Modifier"
              aria-label="Modifier"
              className="grid size-8 place-items-center rounded-md text-muted-foreground transition-colors hover:bg-raised hover:text-foreground"
            >
              <Pencil className="size-4" />
            </Link>
            <IconButton label="Régénérer le rendu" disabled={busy || render.isPending} onClick={() => render.mutate(undefined)}>
              <RefreshCw className={cn("size-4", busy && "animate-spin")} />
            </IconButton>
          </div>
          <div className="flex">
            <IconButton
              label="Copier la légende suggérée"
              onClick={() =>
                navigator.clipboard.writeText(postText(clip)).then(
                  () => toast.success("Légende copiée"),
                  () => toast.error("Copie impossible"),
                )
              }
            >
              <Copy className="size-4" />
            </IconButton>
            <IconButton label="Télécharger les sous-titres (.srt)" disabled={!clip.has_srt} href={`${API_URL}/api/clips/${clip.id}/download?kind=srt`}>
              <FileText className="size-4" />
            </IconButton>
            <IconButton label="Télécharger le MP4" disabled={!clip.has_export} href={`${API_URL}/api/clips/${clip.id}/download?kind=export`}>
              <Download className="size-4" />
            </IconButton>
          </div>
        </div>
      </div>
    </article>
  );
}
