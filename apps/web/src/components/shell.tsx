"use client";

import { Clapperboard, FolderKanban, LayoutDashboard, Plus, Settings2 } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useHealth } from "@/lib/queries";
import { cn } from "@/lib/utils";

const NAV = [
  { href: "/", label: "Tableau de bord", icon: LayoutDashboard, exact: true },
  { href: "/projets", label: "Projets", icon: FolderKanban },
  { href: "/clips", label: "Clips", icon: Clapperboard },
  { href: "/parametres", label: "Paramètres", icon: Settings2 },
];

function StatusLine({ ok, label, detail }: { ok: boolean | "warn"; label: string; detail: string }) {
  return (
    <div className="flex items-start gap-2.5">
      <span className={cn("mt-1.5 size-1.5 shrink-0 rounded-full", ok === true ? "bg-ok" : ok === "warn" ? "bg-warn" : "bg-destructive")} />
      <div className="min-w-0">
        <div className="text-xs text-foreground/90">{label}</div>
        <div className="text-[11px] leading-snug text-muted-foreground">{detail}</div>
      </div>
    </div>
  );
}

function SystemStatus() {
  const health = useHealth();
  if (health.isError) {
    return <StatusLine ok={false} label="API injoignable" detail="Lancez scripts/dev.sh" />;
  }
  if (!health.data) {
    return <div className="h-16" />;
  }
  const missing = Object.entries(health.data.dependencies).filter(([, v]) => !v).map(([k]) => k);
  return (
    <div className="space-y-3">
      <StatusLine ok={health.data.worker_alive} label="Worker" detail={health.data.worker_alive ? "Actif" : "Arrêté — aucun job ne sera traité"} />
      <StatusLine
        ok={health.data.openrouter_key_configured ? true : "warn"}
        label="OpenRouter"
        detail={health.data.openrouter_key_configured ? "Clé configurée côté serveur" : "Clé absente — classement heuristique"}
      />
      {missing.length > 0 ? <StatusLine ok={false} label="Dépendances" detail={`Manquant : ${missing.join(", ")}`} /> : null}
    </div>
  );
}

export function Shell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const health = useHealth();
  return (
    <div className="flex min-h-screen">
      <aside className="sticky top-0 flex h-screen w-56 shrink-0 flex-col bg-surface px-3 py-5">
        <Link href="/" className="mb-7 flex items-center gap-2.5 px-2">
          <span className="grid size-6 place-items-center rounded-[5px] bg-primary">
            <span className="size-2 rounded-[2px] bg-primary-foreground" />
          </span>
          <span className="text-[13px] font-semibold tracking-[0.14em]">CLIPFACTORY</span>
        </Link>

        <Link
          href="/nouveau"
          className="mb-5 flex h-9 items-center justify-center gap-2 rounded-md bg-primary text-sm font-medium text-primary-foreground transition-opacity hover:opacity-90"
        >
          <Plus className="size-4" strokeWidth={2.5} />
          Nouveau projet
        </Link>

        <nav className="space-y-0.5">
          {NAV.map(({ href, label, icon: Icon, exact }) => {
            const active = exact ? pathname === href : pathname.startsWith(href);
            return (
              <Link
                key={href}
                href={href}
                className={cn(
                  "flex h-9 items-center gap-2.5 rounded-md px-2.5 text-sm transition-colors",
                  active ? "bg-raised text-foreground" : "text-muted-foreground hover:text-foreground",
                )}
              >
                <Icon className={cn("size-4", active && "text-primary")} strokeWidth={1.75} />
                {label}
              </Link>
            );
          })}
        </nav>

        <div className="mt-auto px-2">
          <div className="mb-3 text-[10px] font-medium tracking-[0.12em] text-muted-foreground/70 uppercase">Système</div>
          <SystemStatus />
        </div>
      </aside>

      <div className="min-w-0 flex-1">
        {health.data?.test_mode ? (
          <div className="bg-warn/15 px-8 py-2 text-xs text-warn">
            <strong className="font-semibold">MODE TEST</strong> — des adaptateurs simulés remplacent l&apos;IA et/ou la transcription. Les résultats
            produits ainsi sont marqués TEST et ne reflètent aucune analyse réelle.
          </div>
        ) : null}
        <main className="mx-auto w-full max-w-[1320px] px-8 pt-8 pb-16">{children}</main>
      </div>
    </div>
  );
}
