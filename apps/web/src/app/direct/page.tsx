"use client";

import { Trash2 } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { Empty, ErrorNote, PageHeader, SectionTitle } from "@/components/bits";
import { ChatPulse } from "@/components/chat-pulse";
import { ClipCard } from "@/components/clip-card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { PROFILE_LABELS, type Tone, chatBuckets, momentStatus, readyToPost, timeOfDay } from "@/lib/format";
import { liveActions, useAction, useLive } from "@/lib/queries";
import type { LiveChannel, LiveMoment, Profile } from "@/lib/types";
import { cn } from "@/lib/utils";

const TONE: Record<Tone, string> = { muted: "text-muted-foreground", info: "text-info", ok: "text-ok", destructive: "text-destructive" };

function channelStatus(channel: LiveChannel, alive: boolean): { label: string; tone: Tone } {
  if (!alive) return { label: "Surveillance arrêtée", tone: "muted" };
  if (!channel.enabled) return { label: "En pause", tone: "muted" };
  if (channel.state === "recording") {
    const since = channel.live_since ? ` · enregistre depuis ${timeOfDay(Date.parse(channel.live_since))}` : "";
    return { label: `En direct${since}`, tone: "ok" };
  }
  if (channel.state === "error") return { label: "Erreur d'enregistrement — nouvelle tentative automatique", tone: "destructive" };
  return { label: "Hors ligne · en attente du prochain direct", tone: "muted" };
}

/** What chat did, in the words used everywhere on this page. */
function chatFact(moment: LiveMoment): string {
  return `${moment.chatters} personnes en 10 s (habituel : ${Math.round(moment.baseline)})`;
}

function ChannelCard({ channel, moments, alive }: { channel: LiveChannel; moments: LiveMoment[]; alive: boolean }) {
  const toggle = useAction((enabled: boolean) => liveActions.patch(channel.id, { enabled }));
  const remove = useAction(() => liveActions.remove(channel.id), { success: "Chaîne retirée" });
  const buckets = chatBuckets(
    channel.chat,
    moments.map((m) => Date.parse(m.peak_at) / 1000),
  );
  const latest = buckets.at(-1);
  const status = channelStatus(channel, alive);

  return (
    <article className="rounded-xl bg-surface p-5">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <a href={channel.url} target="_blank" rel="noreferrer" className="block truncate text-[15px] font-semibold hover:underline">
            {channel.login}
          </a>
          <div className={cn("mt-1 flex items-center gap-2 text-xs", TONE[status.tone])}>
            <span className={cn("size-1.5 shrink-0 rounded-full bg-current", status.tone === "ok" && "animate-pulse")} />
            {status.label}
          </div>
          {channel.state_detail ? <div className="mt-1 text-[11px] break-words text-muted-foreground">{channel.state_detail}</div> : null}
        </div>
        <div className="flex shrink-0 items-center gap-3">
          <span className="text-[11px] text-muted-foreground">{PROFILE_LABELS[channel.profile]}</span>
          <Switch
            checked={channel.enabled}
            disabled={toggle.isPending}
            onCheckedChange={(enabled) => toggle.mutate(enabled)}
            aria-label={channel.enabled ? `Mettre ${channel.login} en pause` : `Reprendre la surveillance de ${channel.login}`}
          />
          <button
            type="button"
            title="Retirer la chaîne"
            aria-label={`Retirer ${channel.login}`}
            disabled={remove.isPending}
            className="grid size-8 place-items-center rounded-md text-muted-foreground transition-colors hover:bg-raised hover:text-destructive disabled:opacity-40"
            onClick={() => {
              if (window.confirm(`Ne plus surveiller ${channel.login} ? Les clips déjà créés sont conservés.`)) remove.mutate(undefined);
            }}
          >
            <Trash2 className="size-4" />
          </button>
        </div>
      </div>

      <div className="mt-4 h-16">
        {buckets.length > 0 ? (
          <ChatPulse buckets={buckets} />
        ) : (
          <div className="grid h-full place-items-center rounded-md bg-raised/60 text-xs text-muted-foreground">
            {alive && channel.enabled ? "Lecture du chat…" : "Le chat n'est pas lu pour l'instant"}
          </div>
        )}
      </div>
      <div className="mt-2 flex items-baseline justify-between text-[11px] text-muted-foreground">
        <span>Chat · personnes différentes par tranche de 10 s, sur 10 minutes</span>
        {latest ? <span className="tnum shrink-0">{latest.chatters === null ? "chat non lu" : `${latest.chatters} en ce moment`}</span> : null}
      </div>
    </article>
  );
}

function AddChannel() {
  const [channel, setChannel] = useState("");
  const [profile, setProfile] = useState<Profile>("gta6");
  const [rights, setRights] = useState(false);
  const add = useAction(() => liveActions.add({ channel, rights_confirmed: rights, profile }), {
    success: "Chaîne ajoutée — la surveillance démarre",
    onDone: () => {
      setChannel("");
      setRights(false);
    },
  });

  return (
    <form
      className="rounded-xl bg-surface p-5"
      onSubmit={(event) => {
        event.preventDefault();
        add.mutate(undefined);
      }}
    >
      <div className="text-[15px] font-semibold">Ajouter une chaîne</div>
      <div className="mt-3 grid grid-cols-[minmax(0,1fr)_10.5rem] gap-3">
        <Input
          value={channel}
          onChange={(e) => setChannel(e.target.value)}
          placeholder="marlon ou https://twitch.tv/marlon"
          aria-label="Chaîne Twitch"
          autoComplete="off"
          spellCheck={false}
          className="h-9 border-0 bg-raised"
        />
        <select className="field-select" value={profile} onChange={(e) => setProfile(e.target.value as Profile)} aria-label="Profil de contenu">
          {Object.entries(PROFILE_LABELS).map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
      </div>
      <label className="mt-3 flex cursor-pointer items-start gap-3 rounded-lg bg-raised p-3 text-[13px] leading-snug">
        <input type="checkbox" checked={rights} onChange={(e) => setRights(e.target.checked)} className="mt-0.5 size-4 accent-[var(--primary)]" />
        <span>
          Le streamer m&apos;a autorisé(e) à enregistrer cette chaîne et à publier des clips.
          <span className="mt-0.5 block text-muted-foreground">Le chat est lu sans compte ; les coupures pub ne sont pas enregistrées.</span>
        </span>
      </label>
      <Button type="submit" className="mt-3" disabled={!channel.trim() || !rights || add.isPending}>
        Surveiller
      </Button>
    </form>
  );
}

function MomentRow({ moment }: { moment: LiveMoment }) {
  const status = momentStatus(moment);
  const cells = (
    <>
      <span className="tnum font-mono text-xs text-muted-foreground">{timeOfDay(Date.parse(moment.peak_at))}</span>
      <span className="truncate font-medium">{moment.login}</span>
      <span className="tnum truncate text-muted-foreground">{chatFact(moment)}</span>
      <span className={cn("flex min-w-0 items-center justify-end gap-2 text-right text-xs", TONE[status.tone])}>
        <span className={cn("size-1.5 shrink-0 rounded-full bg-current", status.tone === "info" && "animate-pulse")} />
        <span className="line-clamp-2">{status.label}</span>
      </span>
    </>
  );
  const layout = "grid grid-cols-[4rem_minmax(0,9rem)_minmax(0,1fr)_minmax(0,16rem)] items-center gap-4 py-3 text-sm";
  return (
    <li>
      {moment.project_id ? (
        <Link href={`/projets/${moment.project_id}`} className={cn(layout, "hover:bg-surface/60")}>
          {cells}
        </Link>
      ) : (
        <div className={layout}>{cells}</div>
      )}
    </li>
  );
}

export default function LivePage() {
  const { data, isLoading, error } = useLive();
  const header = (
    <PageHeader
      title="Direct"
      description="Les chaînes ci-dessous sont enregistrées dès qu'elles passent en direct. Quand le chat s'emballe par rapport à son niveau habituel, la minute autour du pic devient un clip. La publication reste manuelle : légende à copier et MP4 à télécharger sur chaque clip."
    />
  );

  if (isLoading) {
    return (
      <>
        {header}
        <Skeleton className="h-48 rounded-xl" />
      </>
    );
  }
  if (error || !data) {
    return (
      <>
        {header}
        <Empty title="Impossible de charger le direct">{error?.message}</Empty>
      </>
    );
  }

  const ready = data.moments.filter(readyToPost);
  const underway = data.moments.filter((m) => !readyToPost(m));

  return (
    <>
      {header}

      {!data.alive ? (
        <div className="mb-8">
          <ErrorNote title="La surveillance est arrêtée">
            Aucune chaîne n&apos;est enregistrée et aucun chat n&apos;est lu. Lancez <code className="font-mono text-xs">scripts/dev.sh</code> : le processus de
            surveillance démarre avec le reste.
          </ErrorNote>
        </div>
      ) : null}

      {ready.length > 0 ? (
        <section className="mb-10">
          <SectionTitle aside={<span className="text-xs text-muted-foreground">les plus récents d&apos;abord</span>}>Clips à poster</SectionTitle>
          <div className="grid grid-cols-2 gap-5 lg:grid-cols-3 xl:grid-cols-4">
            {ready.map((moment) =>
              moment.clip ? (
                <div key={moment.id} className="flex flex-col gap-2">
                  <div className="text-xs">
                    <span className="font-medium">{moment.login}</span>
                    <span className="tnum text-muted-foreground">
                      {" "}
                      · {timeOfDay(Date.parse(moment.peak_at))} · {chatFact(moment)}
                    </span>
                  </div>
                  <ClipCard clip={moment.clip} />
                </div>
              ) : null,
            )}
          </div>
        </section>
      ) : null}

      <section>
        <SectionTitle>Chaînes surveillées</SectionTitle>
        <div className="grid gap-5 lg:grid-cols-2">
          {data.channels.map((channel) => (
            <ChannelCard key={channel.id} channel={channel} alive={data.alive} moments={data.moments.filter((m) => m.channel_id === channel.id)} />
          ))}
          <AddChannel />
        </div>
      </section>

      <section className="mt-10">
        <SectionTitle>Moments en cours</SectionTitle>
        {underway.length > 0 ? (
          <ul className="divide-y divide-border">
            {underway.map((moment) => (
              <MomentRow key={moment.id} moment={moment} />
            ))}
          </ul>
        ) : (
          <p className="text-sm text-muted-foreground">
            {data.channels.length === 0
              ? "Ajoutez une chaîne : ses pics de chat apparaîtront ici, puis leurs clips en haut de page."
              : "Aucun pic en cours de traitement. Dès que le chat d'une chaîne en direct s'emballe, le moment apparaît ici."}
          </p>
        )}
      </section>
    </>
  );
}
