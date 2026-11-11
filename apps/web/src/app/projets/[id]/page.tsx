"use client";

import { Download, RotateCcw, Trash2 } from "lucide-react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useMemo, useState } from "react";
import { Empty, ErrorNote, PageHeader, SectionTitle, Segmented, TestBadge } from "@/components/bits";
import { ClipCard } from "@/components/clip-card";
import { PipelineView } from "@/components/pipeline-view";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { API_URL, api, mediaUrl } from "@/lib/api";
import { PRESET_LABELS, PROFILE_LABELS, bytes, clock, dateTime } from "@/lib/format";
import { useAction, useProject } from "@/lib/queries";
import type { Clip } from "@/lib/types";
import { cn } from "@/lib/utils";

type Sort = "score" | "chrono" | "duration";
type Filter = "all" | "pending" | "approved" | "rejected";

export default function ProjectPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const { data: project, error, isLoading } = useProject(id);
  const [sort, setSort] = useState<Sort>("score");
  const [filter, setFilter] = useState<Filter>("all");
  const [showCandidates, setShowCandidates] = useState(false);
  const rerun = useAction(() => api(`/projects/${id}/rerun`, { method: "POST", body: { from_stage: "analyze" } }), { success: "Analyse relancée — la transcription et les signaux sont réutilisés" });
  const remove = useAction(() => api(`/projects/${id}`, { method: "DELETE" }), { success: "Projet supprimé", onDone: () => router.push("/projets") });

  const clips = useMemo(() => {
    const list = (project?.clips ?? []).filter((c: Clip) => filter === "all" || c.approval === filter);
    const by: Record<Sort, (a: Clip, b: Clip) => number> = {
      score: (a, b) => b.score - a.score,
      chrono: (a, b) => a.start_ms - b.start_ms,
      duration: (a, b) => b.duration_ms - a.duration_ms,
    };
    return [...list].sort(by[sort]);
  }, [project?.clips, sort, filter]);

  if (isLoading) return <Skeleton className="h-64 rounded-xl" />;
  if (error || !project) {
    return <Empty title="Projet introuvable" action={<Link href="/projets" className="text-sm text-primary hover:underline">Retour aux projets</Link>}>{error?.message}</Empty>;
  }

  const { source, job, settings } = project;
  const active = project.status === "queued" || project.status === "running";
  const allClips = project.clips ?? [];
  const exported = allClips.filter((c) => c.has_export).length;
  const approved = allClips.filter((c) => c.approval === "approved" && c.has_export).length;
  const rejectedCandidates = (project.candidates ?? []).filter((c) => c.status === "rejected");

  return (
    <>
      <PageHeader
        title={project.name}
        description={
          <span className="flex flex-wrap items-center gap-x-3 gap-y-1">
            {project.is_test ? <TestBadge /> : null}
            <span>{PROFILE_LABELS[settings.profile]}</span>
            <span>Préréglage {PRESET_LABELS[settings.preset].toLowerCase()}</span>
            <span>{settings.clip_count} clip(s) demandé(s), {settings.min_len_s}–{settings.max_len_s} s</span>
            <span>Créé le {dateTime(project.created_at)}</span>
          </span>
        }
        actions={
          <>
            <Button variant="secondary" size="sm" disabled={active || rerun.isPending} onClick={() => rerun.mutate(undefined)} title="Relance l'analyse et le classement en réutilisant la transcription et les signaux">
              <RotateCcw className="size-3.5" /> Relancer l&apos;analyse
            </Button>
            <Button variant="ghost" size="icon-sm" disabled={active || remove.isPending} aria-label="Supprimer le projet" onClick={() => { if (window.confirm("Supprimer ce projet et tous ses clips ?")) remove.mutate(undefined); }}>
              <Trash2 className="size-4" />
            </Button>
          </>
        }
      />

      <section className="mb-6 flex items-center gap-5 rounded-xl bg-surface p-4">
        {source.thumbnail_url ? (
          // eslint-disable-next-line @next/next/no-img-element -- served by the local API
          <img src={mediaUrl(source.thumbnail_url)} alt="" className="aspect-video w-40 shrink-0 rounded-lg object-cover" />
        ) : (
          <div className="aspect-video w-40 shrink-0 rounded-lg bg-raised" />
        )}
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm font-medium">{source.title}</div>
          <div className="mt-0.5 text-xs text-muted-foreground">{source.kind === "youtube" ? `YouTube${source.channel ? ` · ${source.channel}` : ""}` : `Fichier local · ${source.original_filename ?? ""}`}</div>
          {source.status === "ready" ? (
            <dl className="tnum mt-3 flex flex-wrap gap-x-7 gap-y-1 font-mono text-[11px] text-muted-foreground">
              <div>{clock(source.duration_ms)}</div>
              <div>{source.width}×{source.height}</div>
              <div>{source.fps} i/s</div>
              <div>{source.video_codec} / {source.audio_codec ?? "sans audio"}{source.audio_channels ? ` (${source.audio_channels} c.)` : ""}</div>
              <div>{bytes(source.bytes)}</div>
            </dl>
          ) : (
            <div className="mt-3 text-[11px] text-muted-foreground">Métadonnées disponibles après l&apos;import.</div>
          )}
        </div>
      </section>

      {job ? <PipelineView job={job} clipCount={allClips.length} /> : <ErrorNote title="Aucun job pour ce projet" />}

      {project.summary?.explanation && !active ? (
        <p className={cn("mt-6 rounded-lg px-4 py-3 text-sm", (project.summary.selected ?? 0) < (project.summary.requested ?? 0) ? "bg-warn/10 text-warn" : "bg-surface text-muted-foreground")}>
          {project.summary.explanation}
          {project.summary.basis === "heuristic" ? " Classement heuristique : aucune analyse IA n'a été effectuée." : ""}
        </p>
      ) : null}

      <section className="mt-10">
        <div className="mb-4 flex flex-wrap items-center justify-between gap-4">
          <SectionTitle>Clips ({allClips.length})</SectionTitle>
          <div className="flex flex-wrap items-center gap-3">
            <Segmented value={filter} onChange={setFilter} options={[{ value: "all", label: "Tous" }, { value: "pending", label: "À revoir" }, { value: "approved", label: "Approuvés" }, { value: "rejected", label: "Rejetés" }]} />
            <Segmented value={sort} onChange={setSort} options={[{ value: "score", label: "Score" }, { value: "chrono", label: "Chronologie" }, { value: "duration", label: "Durée" }]} />
            <a
              href={`${API_URL}/api/clips-zip?project_id=${id}${approved > 0 ? "&approved_only=true" : ""}`}
              aria-disabled={exported === 0}
              className={cn("inline-flex h-9 items-center gap-2 rounded-md bg-raised px-3 text-xs font-medium hover:bg-accent", exported === 0 && "pointer-events-none opacity-40")}
            >
              <Download className="size-3.5" /> ZIP {approved > 0 ? `(${approved} approuvés)` : `(${exported})`}
            </a>
          </div>
        </div>
        {clips.length > 0 ? (
          <div className="grid grid-cols-2 gap-5 lg:grid-cols-3 xl:grid-cols-4">{clips.map((clip) => <ClipCard key={clip.id} clip={clip} />)}</div>
        ) : active ? (
          <Empty title="Les clips apparaîtront ici">Ils sont créés à l&apos;étape « Affinage des clips », puis rendus un par un.</Empty>
        ) : (
          <Empty title={allClips.length ? "Aucun clip pour ce filtre" : "Aucun clip produit"}>{allClips.length ? undefined : project.summary?.explanation}</Empty>
        )}
      </section>

      {rejectedCandidates.length > 0 ? (
        <section className="mt-10">
          <button type="button" className="text-xs text-muted-foreground hover:text-foreground" onClick={() => setShowCandidates((v) => !v)}>
            {showCandidates ? "Masquer" : "Voir"} les {rejectedCandidates.length} candidats non retenus
          </button>
          {showCandidates ? (
            <ul className="mt-3 divide-y divide-border text-[13px]">
              {rejectedCandidates.map((c) => (
                <li key={c.id} className="grid grid-cols-[3rem_8rem_minmax(0,1fr)_minmax(0,1fr)] items-baseline gap-4 py-2.5">
                  <span className="tnum font-semibold text-muted-foreground">{c.final_score !== null ? Math.round(c.final_score) : "—"}</span>
                  <span className="tnum font-mono text-xs text-muted-foreground">{clock(c.start_ms)} → {clock(c.end_ms)}</span>
                  <span className="truncate">{c.title || c.event_type}</span>
                  <span className="truncate text-muted-foreground">{c.reject_reason}</span>
                </li>
              ))}
            </ul>
          ) : null}
        </section>
      ) : null}
    </>
  );
}
