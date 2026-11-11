"use client";

import { AlertTriangle, Loader2 } from "lucide-react";
import { BASIS_LABELS, JOB_STATUS_LABELS } from "@/lib/format";
import { cn } from "@/lib/utils";

export function PageHeader({ title, description, actions }: { title: string; description?: React.ReactNode; actions?: React.ReactNode }) {
  return (
    <header className="mb-8 flex items-end justify-between gap-6">
      <div className="min-w-0">
        <h1 className="truncate text-2xl font-semibold tracking-tight">{title}</h1>
        {description ? <p className="mt-1.5 max-w-2xl text-sm text-muted-foreground">{description}</p> : null}
      </div>
      {actions ? <div className="flex shrink-0 items-center gap-2">{actions}</div> : null}
    </header>
  );
}

export function SectionTitle({ children, aside }: { children: React.ReactNode; aside?: React.ReactNode }) {
  return (
    <div className="mb-3 flex items-baseline justify-between gap-4">
      <h2 className="text-[11px] font-medium tracking-[0.12em] text-muted-foreground uppercase">{children}</h2>
      {aside}
    </div>
  );
}

const TONE: Record<string, string> = {
  queued: "text-muted-foreground",
  running: "text-info",
  succeeded: "text-ok",
  failed: "text-destructive",
  cancelled: "text-muted-foreground",
  none: "text-muted-foreground",
};

export function JobStatus({ status }: { status: string }) {
  return (
    <span className={cn("inline-flex items-center gap-1.5 text-xs font-medium", TONE[status] ?? "text-muted-foreground")}>
      {status === "running" ? <Loader2 className="size-3 animate-spin" /> : <span className="size-1.5 rounded-full bg-current" />}
      {JOB_STATUS_LABELS[status] ?? status}
    </span>
  );
}

export function TestBadge() {
  return <span className="rounded bg-warn/15 px-1.5 py-0.5 text-[10px] font-semibold tracking-wide text-warn">TEST</span>;
}

/** The editorial score: an estimate of short-form potential, never a probability of virality. */
export function Score({ value, basis, size = "md" }: { value: number; basis: string; size?: "md" | "lg" }) {
  const tone = basis === "test" ? "text-warn" : value >= 75 ? "text-primary" : value >= 55 ? "text-foreground" : "text-muted-foreground";
  return (
    <div className={cn("flex", size === "lg" ? "flex-col gap-0.5" : "items-baseline gap-1.5")} title="Score éditorial (0–100) : estimation du potentiel en format court, pas une probabilité de viralité.">
      <span className={cn("tnum font-semibold tracking-tight", tone, size === "lg" ? "text-4xl" : "text-xl")}>{Math.round(value)}</span>
      <span className="text-[10px] font-medium tracking-wide text-muted-foreground uppercase">{BASIS_LABELS[basis] ?? basis}</span>
    </div>
  );
}

/**
 * A labelled control. Single inputs are wrapped in a <label>; a group of buttons (``group``) gets a
 * named group instead, so each button keeps its own accessible name.
 */
export function Field({ label, hint, children, className, group }: { label: string; hint?: React.ReactNode; children: React.ReactNode; className?: string; group?: boolean }) {
  const heading = (
    <span className="mb-1.5 flex items-baseline justify-between gap-3">
      <span className="text-xs font-medium text-foreground/85">{label}</span>
      {hint ? <span className="text-[11px] text-muted-foreground">{hint}</span> : null}
    </span>
  );
  if (group) {
    return (
      <div role="group" aria-label={label} className={cn("block", className)}>
        {heading}
        {children}
      </div>
    );
  }
  return (
    <label className={cn("block", className)}>
      {heading}
      {children}
    </label>
  );
}

export function Segmented<T extends string>({
  value,
  onChange,
  options,
  className,
}: {
  value: T;
  onChange: (value: T) => void;
  options: Array<{ value: T; label: string; disabled?: boolean; title?: string }>;
  className?: string;
}) {
  return (
    <div className={cn("inline-flex rounded-md bg-raised p-0.5", className)} role="radiogroup">
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          role="radio"
          aria-checked={value === option.value}
          disabled={option.disabled}
          title={option.title}
          onClick={() => onChange(option.value)}
          className={cn(
            "h-8 flex-1 rounded-[5px] px-3 text-xs font-medium whitespace-nowrap transition-colors disabled:opacity-40",
            value === option.value ? "bg-background text-foreground shadow-sm" : "text-muted-foreground hover:text-foreground",
          )}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}

export function Empty({ title, children, action }: { title: string; children?: React.ReactNode; action?: React.ReactNode }) {
  return (
    <div className="rounded-lg bg-surface px-6 py-14 text-center">
      <div className="text-sm font-medium">{title}</div>
      {children ? <p className="mx-auto mt-1.5 max-w-md text-sm text-muted-foreground">{children}</p> : null}
      {action ? <div className="mt-5 flex justify-center">{action}</div> : null}
    </div>
  );
}

export function ErrorNote({ title, children }: { title: string; children?: React.ReactNode }) {
  return (
    <div className="flex gap-3 rounded-lg bg-destructive/10 px-4 py-3 text-sm">
      <AlertTriangle className="mt-0.5 size-4 shrink-0 text-destructive" />
      <div className="min-w-0">
        <div className="font-medium text-destructive">{title}</div>
        {children ? <div className="mt-1 text-[13px] break-words text-foreground/80">{children}</div> : null}
      </div>
    </div>
  );
}

export function Bar({ value, tone = "primary" }: { value: number | null; tone?: "primary" | "info" }) {
  // null = progress cannot be measured: show an indeterminate shimmer instead of an invented percentage.
  return (
    <div className="h-1 w-full overflow-hidden rounded-full bg-raised">
      {value === null ? (
        <div className="h-full w-1/3 animate-pulse rounded-full bg-info/60" />
      ) : (
        <div className={cn("h-full rounded-full transition-[width] duration-500", tone === "info" ? "bg-info" : "bg-primary")} style={{ width: `${Math.round(value * 100)}%` }} />
      )}
    </div>
  );
}
