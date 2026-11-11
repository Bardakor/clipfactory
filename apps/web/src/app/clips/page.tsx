"use client";

import { Download } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { Empty, PageHeader, Segmented } from "@/components/bits";
import { ClipCard } from "@/components/clip-card";
import { Skeleton } from "@/components/ui/skeleton";
import { API_URL } from "@/lib/api";
import { useClips } from "@/lib/queries";
import { cn } from "@/lib/utils";

type Approval = "all" | "pending" | "approved" | "rejected";
type Sort = "score" | "recent" | "duration";

export default function ClipsPage() {
  const [approval, setApproval] = useState<Approval>("all");
  const [sort, setSort] = useState<Sort>("score");
  const { data: clips, isLoading, error } = useClips({ approval: approval === "all" ? undefined : approval, sort });
  const downloadable = (clips ?? []).filter((c) => c.has_export);

  return (
    <>
      <PageHeader
        title="Clips"
        description="Tous les clips, tous projets confondus. Le score est une estimation éditoriale du potentiel en format court — pas une probabilité de viralité."
        actions={
          <a
            href={`${API_URL}/api/clips-zip?ids=${downloadable.map((c) => c.id).join(",")}`}
            className={cn("inline-flex h-9 items-center gap-2 rounded-md bg-raised px-3 text-xs font-medium hover:bg-accent", downloadable.length === 0 && "pointer-events-none opacity-40")}
          >
            <Download className="size-3.5" /> Télécharger la sélection en ZIP ({downloadable.length})
          </a>
        }
      />
      <div className="mb-5 flex flex-wrap gap-3">
        <Segmented value={approval} onChange={setApproval} options={[{ value: "all", label: "Tous" }, { value: "pending", label: "À revoir" }, { value: "approved", label: "Approuvés" }, { value: "rejected", label: "Rejetés" }]} />
        <Segmented value={sort} onChange={setSort} options={[{ value: "score", label: "Score" }, { value: "recent", label: "Récents" }, { value: "duration", label: "Durée" }]} />
      </div>
      {isLoading ? (
        <Skeleton className="h-96 rounded-xl" />
      ) : error ? (
        <Empty title="Impossible de charger les clips">{error.message}</Empty>
      ) : !clips?.length ? (
        <Empty title="Aucun clip" action={approval === "all" ? <Link href="/nouveau" className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground">Nouveau projet</Link> : undefined}>
          {approval === "all" ? "Les clips générés apparaîtront ici." : "Aucun clip ne correspond à ce filtre."}
        </Empty>
      ) : (
        <div className="grid grid-cols-2 gap-5 lg:grid-cols-3 xl:grid-cols-4">{clips.map((clip) => <ClipCard key={clip.id} clip={clip} showProject />)}</div>
      )}
    </>
  );
}
