"use client";

import { Check, Circle, Loader2, Minus, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { STAGE_LABELS, elapsedMs, seconds, usd } from "@/lib/format";
import { jobActions, useAction, useJobLogs } from "@/lib/queries";
import type { Job, Stage } from "@/lib/types";
import { cn } from "@/lib/utils";
import { Bar, ErrorNote, JobStatus, SectionTitle } from "./bits";

function useNow(active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [active]);
  return now;
}

function StageIcon({ status }: { status: Stage["status"] }) {
  const base = "grid size-5 shrink-0 place-items-center rounded-full";
  if (status === "succeeded") return <span className={cn(base, "bg-ok/15 text-ok")}><Check className="size-3" strokeWidth={3} /></span>;
  if (status === "running") return <span className={cn(base, "bg-info/15 text-info")}><Loader2 className="size-3 animate-spin" strokeWidth={3} /></span>;
  if (status === "failed") return <span className={cn(base, "bg-destructive/15 text-destructive")}><X className="size-3" strokeWidth={3} /></span>;
  if (status === "cancelled") return <span className={cn(base, "bg-raised text-muted-foreground")}><Minus className="size-3" strokeWidth={3} /></span>;
  return <span className={cn(base, "text-muted-foreground/40")}><Circle className="size-2 fill-current" /></span>;
}

function StageRow({ stage, now }: { stage: Stage; now: number }) {
  const running = stage.status === "running";
  const duration = running ? elapsedMs(stage.started_at, null, now) : stage.duration_ms;
  return (
    <li className="grid grid-cols-[1.25rem_1fr_auto] items-start gap-x-3 py-2">
      <StageIcon status={stage.status} />
      <div className="min-w-0">
        <div className="flex items-baseline gap-2">
          <span className={cn("text-sm", stage.status === "pending" ? "text-muted-foreground" : "text-foreground")}>{STAGE_LABELS[stage.name] ?? stage.name}</span>
          {stage.cache_hit ? <span className="text-[10px] tracking-wide text-muted-foreground uppercase">cache</span> : null}
        </div>
        {running ? (
          <div className="mt-1.5 max-w-sm space-y-1">
            <Bar value={stage.progress} tone="info" />
            <div className="tnum text-[11px] text-muted-foreground">
              {stage.progress !== null ? `${Math.round(stage.progress * 100)} %` : "progression non mesurable"}
              {stage.detail ? ` · ${stage.detail}` : ""}
            </div>
          </div>
        ) : stage.detail && stage.status !== "pending" ? (
          <div className="mt-0.5 truncate text-[11px] text-muted-foreground">{stage.detail}</div>
        ) : null}
        {stage.error ? <div className="mt-1 text-xs text-destructive">{stage.error.message}</div> : null}
      </div>
      <span className="tnum pt-0.5 font-mono text-[11px] text-muted-foreground">{duration !== null ? seconds(duration) : ""}</span>
    </li>
  );
}

export function PipelineView({ job, clipCount }: { job: Job; clipCount: number }) {
  const active = job.status === "running" || job.status === "queued";
  const now = useNow(active);
  const logs = useJobLogs(job.id, active);
  const cancel = useAction(() => jobActions.cancel(job.id), { success: "Annulation demandée" });
  const retry = useAction(() => jobActions.retry(job.id), { success: "Job relancé" });
  const logBox = useRef<HTMLDivElement>(null);
  useEffect(() => {
    logBox.current?.scrollTo({ top: logBox.current.scrollHeight });
  }, [logs.data?.length]);
  const total = elapsedMs(job.started_at, job.finished_at, now);
  const candidates = job.stages.find((s) => s.name === "analyze")?.metrics?.candidates as number | undefined;

  return (
    <section className="rounded-xl bg-surface p-6">
      <div className="mb-5 flex items-center justify-between gap-6">
        <div className="flex items-center gap-6">
          <JobStatus status={job.status} />
          <dl className="flex gap-6 text-xs">
            <div><dt className="text-muted-foreground">Durée</dt><dd className="tnum mt-0.5 font-mono">{total !== null ? seconds(total) : "—"}</dd></div>
            <div><dt className="text-muted-foreground">Candidats</dt><dd className="tnum mt-0.5 font-mono">{candidates ?? "—"}</dd></div>
            <div><dt className="text-muted-foreground">Clips</dt><dd className="tnum mt-0.5 font-mono">{clipCount}</dd></div>
            <div title="Coût réel rapporté par OpenRouter. L'estimation (jetons × prix catalogue) est indiquée à part.">
              <dt className="text-muted-foreground">Coût réel</dt>
              <dd className="tnum mt-0.5 font-mono">
                {usd(job.cost.actual_usd)} <span className="text-muted-foreground">· est. {usd(job.cost.estimated_usd)}</span>
              </dd>
            </div>
          </dl>
        </div>
        <div className="flex gap-2">
          {active ? (
            <Button variant="secondary" size="sm" disabled={cancel.isPending || job.cancel_requested} onClick={() => cancel.mutate(undefined)}>
              {job.cancel_requested ? "Annulation…" : "Annuler"}
            </Button>
          ) : null}
          {job.status === "failed" || job.status === "cancelled" ? (
            <Button size="sm" disabled={retry.isPending} onClick={() => retry.mutate(undefined)}>Reprendre</Button>
          ) : null}
        </div>
      </div>

      {job.error ? (
        <div className="mb-5">
          <ErrorNote title={`Échec à l'étape « ${STAGE_LABELS[job.error.stage ?? ""] ?? job.error.stage} »`}>
            {job.error.message}
            {job.error.details && Object.keys(job.error.details).length > 0 ? (
              <pre className="mt-2 max-h-32 overflow-auto font-mono text-[11px] whitespace-pre-wrap text-muted-foreground">{JSON.stringify(job.error.details, null, 1)}</pre>
            ) : null}
            <div className="mt-2 text-xs text-muted-foreground">Les étapes déjà terminées sont conservées : « Reprendre » repart de celle-ci.</div>
          </ErrorNote>
        </div>
      ) : null}
      {job.warnings.length > 0 ? (
        <ul className="mb-5 space-y-1 rounded-lg bg-warn/10 px-4 py-3 text-[13px] text-warn">
          {job.warnings.map((warning) => <li key={warning}>{warning}</li>)}
        </ul>
      ) : null}

      <div className="grid gap-8 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.1fr)]">
        <div>
          <SectionTitle>Étapes</SectionTitle>
          <ol className="divide-y divide-border">{job.stages.map((stage) => <StageRow key={stage.name} stage={stage} now={now} />)}</ol>
        </div>
        <div className="min-w-0">
          <SectionTitle>Journal d&apos;activité</SectionTitle>
          <div ref={logBox} className="h-[21.5rem] overflow-y-auto rounded-lg bg-background p-3 font-mono text-[11px] leading-relaxed">
            {(logs.data ?? []).map((line) => (
              <div key={line.id} className={cn("flex gap-2", line.level === "error" ? "text-destructive" : line.level === "warning" ? "text-warn" : "text-muted-foreground")}>
                <span className="tnum shrink-0 opacity-60">{new Date(line.ts).toLocaleTimeString("fr-FR")}</span>
                <span className="min-w-0 break-words">{line.message}</span>
              </div>
            ))}
            {logs.data?.length === 0 ? <div className="text-muted-foreground/60">En attente du worker…</div> : null}
          </div>
        </div>
      </div>
    </section>
  );
}
