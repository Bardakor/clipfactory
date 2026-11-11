"use client";

import Link from "next/link";
import { Bar, BarChart, ResponsiveContainer, Tooltip, XAxis } from "recharts";
import { Empty, JobStatus, PageHeader, SectionTitle, TestBadge } from "@/components/bits";
import { Skeleton } from "@/components/ui/skeleton";
import { dateTime, seconds, usd } from "@/lib/format";
import { useStats } from "@/lib/queries";

function Kpi({ label, value, note }: { label: string; value: React.ReactNode; note?: React.ReactNode }) {
  return (
    <div className="min-w-0">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="tnum mt-1.5 text-3xl font-semibold tracking-tight">{value}</div>
      {note ? <div className="mt-1 text-[11px] text-muted-foreground">{note}</div> : null}
    </div>
  );
}

export default function DashboardPage() {
  const { data: stats, isLoading, error } = useStats();

  if (isLoading) {
    return (
      <>
        <PageHeader title="Tableau de bord" />
        <Skeleton className="h-28 rounded-xl" />
      </>
    );
  }
  if (error || !stats) {
    return (
      <>
        <PageHeader title="Tableau de bord" />
        <Empty title="Impossible de charger les statistiques">{error?.message}</Empty>
      </>
    );
  }

  const timed = stats.history.filter((h) => h.processing_s !== null && !h.is_test && h.status === "succeeded").reverse();
  const isFresh = stats.history.length === 0;

  return (
    <>
      <PageHeader
        title="Tableau de bord"
        description="Chiffres mesurés sur vos traitements réels. Les essais faits avec les adaptateurs de test sont comptés à part."
      />

      {isFresh ? (
        <Empty
          title="Aucune vidéo traitée pour l'instant"
          action={
            <Link href="/nouveau" className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:opacity-90">
              Créer un premier projet
            </Link>
          }
        >
          Collez une URL YouTube ou déposez un fichier : CLIPFACTORY transcrit, repère les meilleurs moments, les classe et produit des clips verticaux sous-titrés.
        </Empty>
      ) : (
        <>
          <section className="grid grid-cols-2 gap-x-8 gap-y-8 rounded-xl bg-surface p-6 md:grid-cols-3 xl:grid-cols-6">
            <Kpi label="Sources traitées" value={stats.sources_processed} />
            <Kpi label="Clips rendus" value={stats.clips_rendered} note={`${stats.clips_approved} approuvé(s)`} />
            <Kpi label="En attente de revue" value={stats.clips_awaiting_review} />
            <Kpi
              label="Jobs réussis / échoués"
              value={<>{stats.jobs_succeeded}<span className="text-muted-foreground"> / </span><span className={stats.jobs_failed ? "text-destructive" : ""}>{stats.jobs_failed}</span></>}
              note={stats.jobs_active ? `${stats.jobs_active} en cours` : undefined}
            />
            <Kpi
              label="Coût d'inférence réel"
              value={usd(stats.inference.actual_usd)}
              note={`${stats.inference.requests} requête(s) · estimation catalogue ${usd(stats.inference.estimated_usd)}`}
            />
            <Kpi
              label="Traitement moyen"
              value={stats.avg_processing_s !== null ? seconds(stats.avg_processing_s * 1000) : "—"}
              note={stats.avg_realtime_factor !== null ? `${String(stats.avg_realtime_factor).replace(".", ",")}× la durée de la source` : "pas encore mesuré"}
            />
          </section>

          <div className="mt-10 grid gap-10 lg:grid-cols-[minmax(0,1.6fr)_minmax(0,1fr)]">
            <section>
              <SectionTitle aside={<Link href="/projets" className="text-xs text-muted-foreground hover:text-foreground">Tout voir</Link>}>Historique des traitements</SectionTitle>
              <ul className="divide-y divide-border">
                {stats.history.map((item) => (
                  <li key={`${item.project_id}-${item.created_at}`}>
                    <Link href={`/projets/${item.project_id}`} className="grid grid-cols-[minmax(0,1fr)_7rem_6rem_7rem] items-center gap-4 py-3 text-sm hover:bg-surface/60">
                      <span className="flex min-w-0 items-center gap-2">
                        <span className="truncate">{item.name}</span>
                        {item.is_test ? <TestBadge /> : null}
                      </span>
                      <JobStatus status={item.status} />
                      <span className="tnum text-right font-mono text-xs text-muted-foreground">{item.processing_s !== null ? seconds(item.processing_s * 1000) : "—"}</span>
                      <span className="text-right text-xs text-muted-foreground">{dateTime(item.created_at)}</span>
                    </Link>
                  </li>
                ))}
              </ul>
            </section>

            <section>
              <SectionTitle>Durée de traitement par exécution</SectionTitle>
              {timed.length >= 2 ? (
                <div className="h-48 rounded-xl bg-surface p-4">
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart data={timed.map((h) => ({ name: h.name.slice(0, 14), secondes: h.processing_s }))}>
                      <XAxis dataKey="name" tick={{ fill: "var(--muted-foreground)", fontSize: 10 }} axisLine={false} tickLine={false} />
                      <Tooltip
                        cursor={{ fill: "var(--raised)" }}
                        contentStyle={{ background: "var(--popover)", border: "none", borderRadius: 8, fontSize: 12 }}
                        formatter={(value) => [seconds(Number(value) * 1000), "Durée"]}
                      />
                      <Bar dataKey="secondes" fill="var(--primary)" radius={[3, 3, 0, 0]} maxBarSize={28} />
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              ) : (
                <p className="text-sm text-muted-foreground">Le graphique apparaît à partir de deux traitements réels terminés.</p>
              )}
              {stats.test_projects > 0 ? (
                <p className="mt-4 text-xs text-muted-foreground">{stats.test_projects} projet(s) de test exclus de ces chiffres.</p>
              ) : null}
            </section>
          </div>
        </>
      )}
    </>
  );
}
