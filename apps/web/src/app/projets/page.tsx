"use client";

import Link from "next/link";
import { Empty, JobStatus, PageHeader, TestBadge } from "@/components/bits";
import { Skeleton } from "@/components/ui/skeleton";
import { mediaUrl } from "@/lib/api";
import { PROFILE_LABELS, STAGE_LABELS, clock, dateTime } from "@/lib/format";
import { useProjects } from "@/lib/queries";

export default function ProjectsPage() {
  const { data: projects, isLoading, error } = useProjects();
  return (
    <>
      <PageHeader title="Projets" description="Chaque projet associe une vidéo source à un jeu de réglages. Relancer un projet réutilise la transcription et les signaux déjà calculés." />
      {isLoading ? (
        <Skeleton className="h-40 rounded-xl" />
      ) : error ? (
        <Empty title="Impossible de charger les projets">{error.message}</Empty>
      ) : !projects?.length ? (
        <Empty title="Aucun projet" action={<Link href="/nouveau" className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground">Nouveau projet</Link>} />
      ) : (
        <ul className="divide-y divide-border">
          {projects.map((project) => (
            <li key={project.id}>
              <Link href={`/projets/${project.id}`} className="grid grid-cols-[6rem_minmax(0,1fr)_9rem_11rem_5rem_7rem] items-center gap-5 py-3 hover:bg-surface/60">
                {project.source.thumbnail_url ? (
                  // eslint-disable-next-line @next/next/no-img-element -- served by the local API
                  <img src={mediaUrl(project.source.thumbnail_url)} alt="" className="aspect-video w-24 rounded-md object-cover" />
                ) : (
                  <div className="aspect-video w-24 rounded-md bg-surface" />
                )}
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="truncate text-sm font-medium">{project.name}</span>
                    {project.is_test ? <TestBadge /> : null}
                  </div>
                  <div className="tnum mt-0.5 text-xs text-muted-foreground">{clock(project.source.duration_ms)} · {PROFILE_LABELS[project.settings.profile]}</div>
                </div>
                <JobStatus status={project.status} />
                <span className="truncate text-xs text-muted-foreground">{project.current_stage ? STAGE_LABELS[project.current_stage] : ""}</span>
                <span className="tnum text-right text-sm">{project.clip_count} <span className="text-xs text-muted-foreground">clips</span></span>
                <span className="text-right text-xs text-muted-foreground">{dateTime(project.created_at)}</span>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </>
  );
}
