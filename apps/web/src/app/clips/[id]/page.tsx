"use client";

import { ArrowLeft, Check, Copy, Download, Loader2, X } from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { Bar, Empty, ErrorNote, Field, Score, SectionTitle, Segmented, TestBadge } from "@/components/bits";
import { RejectMenu } from "@/components/clip-card";
import { TrimTimeline } from "@/components/trim-timeline";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { API_URL, api, mediaUrl } from "@/lib/api";
import { buildPatch, useEditor } from "@/lib/editor-store";
import { APPROVAL_LABELS, FACECAM_POSITION_LABELS, LAYOUT_LABELS, RENDER_STATUS_LABELS, STAGE_LABELS, STYLE_LABELS, clock, timecode } from "@/lib/format";
import { clipActions, useAction, useClip, useTranscript } from "@/lib/queries";
import type { ClipDetail, FacecamPosition, Layout, SubtitleStyle } from "@/lib/types";
import { cn } from "@/lib/utils";

const CHECK_LABELS: Record<string, string> = {
  file_exists: "Fichier présent",
  probe: "Lisible par ffprobe",
  h264: "Vidéo H.264",
  aac: "Audio AAC",
  dimensions: "Dimensions exactes",
  vertical: "Format vertical",
  frame_rate: "Cadence d'images",
  duration: "Durée conforme",
  audio_stream: "Piste audio présente",
  frames: "Nombre d'images",
  decodable: "Décodage complet",
  captions_in_range: "Sous-titres dans la durée",
  av_sync: "Synchro audio/vidéo",
};

function Panel({ title, aside, children }: { title: string; aside?: React.ReactNode; children: React.ReactNode }) {
  return (
    <section className="rounded-xl bg-surface p-5">
      <SectionTitle aside={aside}>{title}</SectionTitle>
      {children}
    </section>
  );
}

function ScoreBreakdown({ detail }: { detail: ClipDetail }) {
  const { evidence, penalties, clip } = detail;
  const rubric = evidence?.rubric;
  return (
    <div className="space-y-3 text-xs">
      <p className="text-[13px] leading-relaxed text-foreground/85">{clip.justification}</p>
      <div className="flex flex-wrap gap-x-5 gap-y-1 text-muted-foreground">
        {Object.entries(evidence?.components ?? {}).map(([name, value]) => (
          <span key={name}>
            {({ heuristic: "Signaux", multimodal: "Visuel", editorial: "Éditorial" } as Record<string, string>)[name] ?? name}{" "}
            <span className="tnum text-foreground">{Math.round(value * 100)}</span>
          </span>
        ))}
        {Object.entries(penalties ?? {}).map(([name, value]) => (
          <span key={name} className="text-warn">
            −{Math.round(value * 100)}{" "}
            {(
              {
                missing_context: "contexte manquant",
                dead_time: "temps mort",
                length: "durée",
                low_confidence: "confiance faible",
                unclear_audio: "audio peu clair",
              } as Record<string, string>
            )[name] ?? name}
          </span>
        ))}
      </div>
      {rubric ? (
        <div className="grid grid-cols-3 gap-x-5 gap-y-1.5">
          {(["hook", "comprehensibility", "surprise", "humor", "emotion", "narrative", "pacing", "payoff", "originality"] as const).map((key) => (
            <div key={key} className="flex items-center gap-2">
              <span className="w-24 shrink-0 text-muted-foreground">
                {
                  {
                    hook: "Accroche",
                    comprehensibility: "Clarté",
                    surprise: "Surprise",
                    humor: "Humour",
                    emotion: "Émotion",
                    narrative: "Récit",
                    pacing: "Rythme",
                    payoff: "Chute",
                    originality: "Originalité",
                  }[key]
                }
              </span>
              <div className="h-1 flex-1 rounded-full bg-raised">
                <div className="h-full rounded-full bg-foreground/60" style={{ width: `${(rubric[key] ?? 0) * 10}%` }} />
              </div>
              <span className="tnum w-4 text-right">{Math.round(rubric[key] ?? 0)}</span>
            </div>
          ))}
        </div>
      ) : null}
      {evidence?.refinement?.length ? <p className="text-muted-foreground">Bornes : {evidence.refinement.join(" · ")}</p> : null}
      <p className="text-muted-foreground/70">Estimation éditoriale à partir de qualités observables — pas une probabilité de viralité.</p>
    </div>
  );
}

function TranscriptEditor({ sourceId, startMs, endMs }: { sourceId: string; startMs: number; endMs: number }) {
  const transcript = useTranscript(sourceId, Math.max(0, startMs - 5000), endMs + 5000);
  const [edits, setEdits] = useState<Record<string, string>>({});
  const save = useAction(
    () =>
      api<{ updated_segments: number; clips_to_rerender: number }>(`/sources/${sourceId}/transcript`, {
        method: "PUT",
        body: { segments: Object.entries(edits).map(([id, text]) => ({ id, text })) },
      }),
    {
      onDone: (result) => {
        setEdits({});
        toast.success(`${result.updated_segments} segment(s) corrigé(s) — ${result.clips_to_rerender} clip(s) à re-rendre, sans nouvelle transcription`);
      },
    },
  );
  if (transcript.isLoading) return <Skeleton className="h-24" />;
  if (!transcript.data) return <p className="text-sm text-muted-foreground">Aucune transcription pour cette source.</p>;
  const { transcript: meta, segments } = transcript.data;
  return (
    <div>
      <p className="mb-3 text-[11px] text-muted-foreground">
        {meta.provider} · {meta.model.split("/").pop()} · {meta.language ?? "langue inconnue"} · précision{" "}
        {meta.precision === "word" ? "au mot" : "à la phrase"}
        {meta.edited ? " · corrigée à la main" : ""}
        {meta.is_test ? " · TEST (simulée)" : ""}
      </p>
      <div className="max-h-72 space-y-1.5 overflow-y-auto pr-1">
        {segments.map((segment) => {
          const inside = segment.end_ms > startMs && segment.start_ms < endMs;
          return (
            <div key={segment.id} className={cn("grid grid-cols-[4.5rem_1fr] items-start gap-3", !inside && "opacity-45")}>
              <span className="tnum pt-2 font-mono text-[11px] text-muted-foreground">{timecode(segment.start_ms)}</span>
              <Textarea
                rows={1}
                value={edits[segment.id] ?? segment.text}
                onChange={(e) => setEdits((prev) => ({ ...prev, [segment.id]: e.target.value }))}
                className="min-h-0 resize-none border-0 bg-raised py-1.5 text-[13px]"
              />
            </div>
          );
        })}
        {segments.length === 0 ? <p className="text-sm text-muted-foreground">Aucune parole dans cet intervalle.</p> : null}
      </div>
      {Object.keys(edits).length > 0 ? (
        <div className="mt-3 flex items-center justify-between gap-3">
          <span className="text-[11px] text-muted-foreground">
            Changer le nombre de mots d&apos;une phrase lui fait perdre son horodatage au mot (pas de surlignage inventé).
          </span>
          <Button size="sm" variant="secondary" disabled={save.isPending} onClick={() => save.mutate(undefined)}>
            Enregistrer la transcription
          </Button>
        </div>
      ) : null}
    </div>
  );
}

export default function ClipEditorPage() {
  const { id } = useParams<{ id: string }>();
  const { data, error, isLoading } = useClip(id);
  const draft = useEditor();
  const [chosenView, setView] = useState<"export" | "preview" | null>(null);
  const clip = data?.clip;
  const loadDraft = draft.load;
  const draftClipId = draft.clipId;

  // Load the draft once per clip; later refetches must not overwrite what the user is typing.
  useEffect(() => {
    if (clip && draftClipId !== clip.id) loadDraft(clip);
  }, [clip, draftClipId, loadDraft]);

  const save = useAction((patch: Record<string, unknown>) => clipActions.patch(id, patch), { onDone: ({ clip: saved }) => draft.load(saved) });
  const render = useAction((mode: "preview" | "final") => clipActions.render(id, mode));
  const feedback = useAction((body: { decision: string; rejection_reason?: string; rating?: number }) => clipActions.feedback(id, body));

  if (isLoading || (clip && draft.clipId !== clip.id)) return <Skeleton className="h-[70vh] rounded-xl" />;
  if (error || !data || !clip) {
    return (
      <Empty
        title="Clip introuvable"
        action={
          <Link href="/clips" className="text-sm text-primary hover:underline">
            Retour aux clips
          </Link>
        }
      >
        {error?.message}
      </Empty>
    );
  }

  // Until the user picks a tab, show the final export when it is current, otherwise the latest preview.
  const view = chosenView ?? (clip.has_export && clip.render_status === "rendered" ? "export" : clip.preview_url ? "preview" : "export");
  const patch = buildPatch(draft, clip);
  const dirty = Object.keys(patch).length > 0;
  const trimmed = draft.startMs !== clip.start_ms || draft.endMs !== clip.end_ms;
  const job = data.active_job;
  const running = job?.stages.find((s) => s.status === "running");
  const videoPath = view === "preview" ? clip.preview_url : clip.video_url;
  const checks = Object.entries(clip.validation?.checks ?? {});

  async function saveThenRender(mode: "preview" | "final") {
    if (dirty) await save.mutateAsync(patch);
    await render.mutateAsync(mode);
    setView(mode === "preview" ? "preview" : "export");
    toast.success(mode === "preview" ? "Aperçu en cours de rendu" : "Rendu final lancé");
  }

  return (
    <>
      <div className="mb-6 flex items-center justify-between gap-6">
        <div className="flex min-w-0 flex-1 items-center gap-3">
          <Link
            href={`/projets/${clip.project_id}`}
            className="grid size-8 shrink-0 place-items-center rounded-md text-muted-foreground hover:bg-surface hover:text-foreground"
            aria-label="Retour au projet"
          >
            <ArrowLeft className="size-4" />
          </Link>
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              <span className="truncate">{data.project.name}</span>
              <span>· clip #{clip.rank}</span>
              {clip.is_test ? <TestBadge /> : null}
            </div>
            <Input
              value={draft.title}
              onChange={(e) => draft.set({ title: e.target.value })}
              aria-label="Titre du clip"
              className="mt-0.5 h-9 border-0 bg-transparent px-0 text-xl font-semibold tracking-tight focus-visible:ring-0"
            />
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <span className="mr-2 text-xs text-muted-foreground">{dirty ? "Modifications non enregistrées" : RENDER_STATUS_LABELS[clip.render_status]}</span>
          <Button variant="ghost" size="sm" disabled={!dirty || save.isPending} onClick={() => save.mutate(patch)}>
            Enregistrer
          </Button>
          <Button variant="secondary" size="sm" disabled={Boolean(job) || render.isPending || save.isPending} onClick={() => void saveThenRender("preview")}>
            Aperçu rapide
          </Button>
          <Button size="sm" disabled={Boolean(job) || render.isPending || save.isPending} onClick={() => void saveThenRender("final")}>
            Rendu final
          </Button>
        </div>
      </div>

      <div className="grid gap-6 lg:grid-cols-[minmax(0,22rem)_minmax(0,1fr)]">
        {/* ── player ── */}
        <div className="space-y-4 lg:sticky lg:top-6 lg:self-start">
          <div className="relative aspect-[9/16] overflow-hidden rounded-xl bg-black">
            {videoPath ? (
              <video key={videoPath} src={mediaUrl(videoPath)} controls playsInline className="size-full object-contain" />
            ) : (
              <div className="grid size-full place-items-center px-8 text-center text-sm text-muted-foreground">
                {view === "preview" ? "Aucun aperçu. Cliquez sur « Aperçu rapide »." : "Pas encore d'export final."}
              </div>
            )}
            {job ? (
              <div className="absolute inset-x-0 bottom-0 bg-black/80 p-4 backdrop-blur">
                <div className="mb-2 flex items-center gap-2 text-xs text-white">
                  <Loader2 className="size-3.5 animate-spin" />
                  {job.payload?.mode === "preview" ? "Aperçu" : "Rendu final"} · {running ? STAGE_LABELS[running.name] : "en file d'attente"}
                  {running?.detail ? <span className="text-white/60">· {running.detail}</span> : null}
                </div>
                <Bar value={running ? running.progress : null} tone="info" />
              </div>
            ) : null}
          </div>
          <div className="flex items-center justify-between">
            <Segmented
              value={view}
              onChange={setView}
              options={[
                { value: "export", label: "Export final", disabled: !clip.has_export },
                { value: "preview", label: "Aperçu", disabled: !clip.preview_url },
              ]}
            />
            <a
              href={`${API_URL}/api/clips/${clip.id}/download?kind=export`}
              className={cn(
                "inline-flex h-8 items-center gap-1.5 rounded-md px-2.5 text-xs text-muted-foreground hover:bg-surface hover:text-foreground",
                !clip.has_export && "pointer-events-none opacity-40",
              )}
            >
              <Download className="size-3.5" /> MP4
            </a>
          </div>
          {clip.render_status === "stale" ? (
            <p className="rounded-lg bg-warn/10 px-3 py-2 text-xs text-warn">
              L&apos;export final ne reflète plus les réglages actuels. Relancez un rendu final.
            </p>
          ) : null}
          {clip.render_status === "failed" ? (
            <ErrorNote title="Dernier rendu en échec">{clip.validation?.error?.message ?? `Validation : ${clip.validation?.failures?.join(", ")}`}</ErrorNote>
          ) : null}
          {checks.length > 0 && clip.has_export ? (
            <details className="rounded-xl bg-surface p-4 text-xs">
              <summary className="cursor-pointer text-muted-foreground">
                Export validé · {String(clip.validation?.details?.width)}×{String(clip.validation?.details?.height)} ·{" "}
                {String(clip.validation?.details?.frames)} images · décalage audio {String(clip.validation?.details?.audio_offset_ms ?? "n/m")} ms
              </summary>
              <ul className="mt-3 grid grid-cols-2 gap-x-3 gap-y-1">
                {checks.map(([name, passed]) => (
                  <li key={name} className={cn("flex items-center gap-1.5", passed ? "text-muted-foreground" : "text-destructive")}>
                    {passed ? <Check className="size-3 text-ok" /> : <X className="size-3" />} {CHECK_LABELS[name] ?? name}
                  </li>
                ))}
              </ul>
            </details>
          ) : null}
        </div>

        {/* ── controls ── */}
        <div className="min-w-0 space-y-5">
          <Panel
            title="Score éditorial"
            aside={
              <div className="flex items-center gap-1">
                <span className="mr-2 text-xs text-muted-foreground">{APPROVAL_LABELS[clip.approval]}</span>
                <Button
                  size="sm"
                  variant={clip.approval === "approved" ? "default" : "secondary"}
                  onClick={() => feedback.mutate({ decision: clip.approval === "approved" ? "reset" : "accepted" })}
                >
                  <Check className="size-3.5" /> Approuver
                </Button>
                <RejectMenu onReject={(reason) => feedback.mutate({ decision: "rejected", rejection_reason: reason })}>
                  {(open) => (
                    <Button
                      size="sm"
                      variant={clip.approval === "rejected" ? "destructive" : "secondary"}
                      onClick={clip.approval === "rejected" ? () => feedback.mutate({ decision: "reset" }) : open}
                    >
                      <X className="size-3.5" /> Rejeter
                    </Button>
                  )}
                </RejectMenu>
              </div>
            }
          >
            <div className="grid gap-6 sm:grid-cols-[auto_1fr]">
              <Score value={clip.score} basis={clip.score_basis} size="lg" />
              <ScoreBreakdown detail={data} />
            </div>
            <div className="mt-4 flex items-center gap-3 text-xs text-muted-foreground">
              Votre note
              <div className="flex gap-1">
                {[1, 2, 3, 4, 5].map((n) => {
                  const current = [...clip.feedback].reverse().find((f) => f.rating !== null)?.rating ?? 0;
                  return (
                    <button
                      key={n}
                      type="button"
                      onClick={() => feedback.mutate({ decision: "rated", rating: n })}
                      className={cn("tnum size-6 rounded text-[11px]", n <= current ? "bg-primary text-primary-foreground" : "bg-raised hover:text-foreground")}
                    >
                      {n}
                    </button>
                  );
                })}
              </div>
            </div>
          </Panel>

          <Panel
            title="Découpe"
            aside={
              trimmed || clip.start_ms !== clip.original_start_ms || clip.end_ms !== clip.original_end_ms ? (
                <button
                  type="button"
                  className="text-xs text-muted-foreground hover:text-foreground"
                  onClick={() => draft.set({ startMs: clip.original_start_ms, endMs: clip.original_end_ms })}
                >
                  Revenir aux bornes d&apos;origine
                </button>
              ) : undefined
            }
          >
            <TrimTimeline
              startMs={draft.startMs}
              endMs={draft.endMs}
              originalStartMs={clip.original_start_ms}
              originalEndMs={clip.original_end_ms}
              sourceDurationMs={data.source.duration_ms}
              onChange={(startMs, endMs) => draft.set({ startMs, endMs, captionEdits: {} })}
            />
            <p className="mt-3 text-[11px] text-muted-foreground">
              Source : {data.source.title} · {clock(data.source.duration_ms)}. Flèches ←/→ sur une poignée : ±0,1 s (Maj : ±1 s). La découpe est ré-encodée à
              l&apos;image près.
            </p>
          </Panel>

          <Panel
            title="Sous-titres"
            aside={
              <span className="text-[11px] text-muted-foreground">
                Synchronisation {clip.captions_precision === "word" ? "au mot" : "à la phrase"}
                {clip.captions_edited ? " · texte modifié" : ""}
              </span>
            }
          >
            <div className="grid gap-5 xl:grid-cols-2">
              <div className="space-y-4">
                <Field group label="Style">
                  <Segmented
                    className="w-full"
                    value={draft.style}
                    onChange={(style: SubtitleStyle) => draft.set({ style })}
                    options={Object.entries(STYLE_LABELS).map(([value, label]) => ({ value: value as SubtitleStyle, label }))}
                  />
                </Field>
                <div className="grid grid-cols-2 gap-4">
                  <Field label="Police">
                    <select
                      className="field-select"
                      value={draft.options.font ?? ""}
                      onChange={(e) => draft.set({ options: { ...draft.options, font: e.target.value || null } })}
                    >
                      <option value="">Celle du style</option>
                      {["Montserrat", "Inter", "Bebas Neue", "Anton"].map((font) => (
                        <option key={font}>{font}</option>
                      ))}
                    </select>
                  </Field>
                  <Field label="Position">
                    <select
                      className="field-select"
                      value={draft.options.position}
                      onChange={(e) => draft.set({ options: { ...draft.options, position: e.target.value as "auto" } })}
                    >
                      <option value="auto">Automatique (sous le gameplay)</option>
                      <option value="top">Haut</option>
                      <option value="middle">Milieu</option>
                      <option value="bottom">Bas</option>
                    </select>
                  </Field>
                </div>
                <Field label="Taille" hint={<span className="tnum">{Math.round(draft.options.scale * 100)} %</span>}>
                  <input
                    type="range"
                    className="field-range"
                    min={0.6}
                    max={1.5}
                    step={0.05}
                    value={draft.options.scale}
                    onChange={(e) => draft.set({ options: { ...draft.options, scale: Number(e.target.value) } })}
                  />
                </Field>
                <div className="flex items-center justify-between gap-4">
                  <label className="flex items-center gap-3 text-xs">
                    <input
                      type="color"
                      value={draft.options.accent ?? "#FFE14D"}
                      onChange={(e) => draft.set({ options: { ...draft.options, accent: e.target.value } })}
                      className="size-7 cursor-pointer rounded border-0 bg-transparent p-0"
                      aria-label="Couleur d'accent"
                    />
                    Couleur d&apos;accent
                    {draft.options.accent ? (
                      <button
                        type="button"
                        className="text-muted-foreground hover:text-foreground"
                        onClick={() => draft.set({ options: { ...draft.options, accent: null } })}
                      >
                        celle du style
                      </button>
                    ) : null}
                  </label>
                  <label className="flex items-center gap-2 text-xs">
                    Surligner le mot prononcé
                    <Switch checked={draft.options.emphasis} onCheckedChange={(emphasis) => draft.set({ options: { ...draft.options, emphasis } })} />
                  </label>
                </div>
              </div>
              <div>
                <div className="mb-1.5 flex items-baseline justify-between">
                  <span className="text-xs font-medium text-foreground/85">Texte à l&apos;écran</span>
                  {clip.captions_edited ? (
                    <button
                      type="button"
                      className="text-[11px] text-muted-foreground hover:text-foreground"
                      onClick={() => save.mutate({ reset_captions: true })}
                    >
                      Régénérer depuis la transcription
                    </button>
                  ) : null}
                </div>
                {trimmed ? (
                  <p className="rounded-lg bg-raised px-3 py-2.5 text-xs text-muted-foreground">
                    Enregistrez la nouvelle découpe : les sous-titres seront recalculés pour cet intervalle, puis modifiables ici.
                  </p>
                ) : clip.caption_pages.length === 0 ? (
                  <p className="text-sm text-muted-foreground">Aucune parole dans ce clip.</p>
                ) : (
                  <div className="max-h-72 space-y-1 overflow-y-auto pr-1">
                    {clip.caption_pages.map((page) => (
                      <div key={`${page.index}-${page.start_ms}`} className="grid grid-cols-[3.2rem_1fr] items-center gap-2">
                        <span
                          className="tnum font-mono text-[11px] text-muted-foreground"
                          title={page.measured ? "Horodatage mesuré au mot" : "Horodatage à la phrase : pas de surlignage mot à mot"}
                        >
                          {(page.start_ms / 1000).toFixed(1)}s{page.measured ? "" : " ~"}
                        </span>
                        <Input
                          value={draft.captionEdits[page.index] ?? page.text}
                          onChange={(e) => draft.editCaption(page.index, e.target.value)}
                          className="h-8 border-0 bg-raised text-[13px]"
                        />
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>
          </Panel>

          <Panel
            title="Mise en page verticale"
            aside={
              clip.resolved_layout ? (
                <span className="text-[11px] text-muted-foreground">
                  Dernier rendu : {LAYOUT_LABELS[clip.resolved_layout.used]} — {clip.resolved_layout.reason}
                </span>
              ) : undefined
            }
          >
            <Segmented
              className="w-full"
              value={draft.layout}
              onChange={(layout: Layout) => draft.set({ layout })}
              options={(["auto", "full", "crop", "split", "dynamic", "manual"] as const).map((value) => ({
                value,
                label: LAYOUT_LABELS[value],
                title: value === "split" && !data.source.facecam_found ? "Aucune facecam fiable détectée : le rendu reviendra au gameplay complet" : undefined,
              }))}
            />
            {draft.layout === "manual" ? (
              <div className="mt-5 grid items-center gap-6 sm:grid-cols-[14rem_1fr]">
                <div className="relative aspect-video rounded-md bg-background ring-1 ring-border">
                  <div
                    className="absolute rounded-sm bg-primary/25 ring-1 ring-primary"
                    style={{ left: `${draft.crop.x * 100}%`, top: `${draft.crop.y * 100}%`, width: `${draft.crop.w * 100}%`, height: `${draft.crop.h * 100}%` }}
                  />
                </div>
                <div className="grid grid-cols-2 gap-x-5 gap-y-3">
                  {(
                    [
                      ["x", "Gauche", 0, 1 - draft.crop.w],
                      ["w", "Largeur", 0.2, 1 - draft.crop.x],
                      ["y", "Haut", 0, 1 - draft.crop.h],
                      ["h", "Hauteur", 0.2, 1 - draft.crop.y],
                    ] as const
                  ).map(([key, label, min, max]) => (
                    <Field key={key} label={label} hint={<span className="tnum">{Math.round(draft.crop[key] * 100)} %</span>}>
                      <input
                        type="range"
                        className="field-range"
                        min={min}
                        max={Math.max(min, max)}
                        step={0.01}
                        value={draft.crop[key]}
                        onChange={(e) => draft.set({ crop: { ...draft.crop, [key]: Number(e.target.value) } })}
                      />
                    </Field>
                  ))}
                </div>
              </div>
            ) : (
              <p className="mt-3 text-[11px] text-muted-foreground">
                {
                  {
                    auto: "Streamer + jeu si une facecam est détectée de façon fiable, sinon gameplay complet.",
                    full: "Gameplay entier sur un fond flouté tiré de la même image.",
                    crop: "Recadre autour de l'action quand sa position est claire, sinon reste centré.",
                    split: "Le streamer et le jeu l'un au-dessus de l'autre, en pleine largeur ; le jeu est recadré au centre.",
                    dynamic: "Change de cadrage uniquement aux changements de plan, jamais en continu.",
                    manual: "",
                  }[draft.layout]
                }
              </p>
            )}
            {data.source.facecam_found && (draft.layout === "auto" || draft.layout === "split") ? (
              <Segmented
                className="mt-4 w-full"
                value={draft.facecamPosition ?? data.project.settings.facecam_position ?? "top"}
                onChange={(facecamPosition: FacecamPosition) => draft.set({ facecamPosition })}
                options={Object.entries(FACECAM_POSITION_LABELS).map(([value, label]) => ({ value: value as FacecamPosition, label }))}
              />
            ) : null}
          </Panel>

          <Panel title="Transcription de la source">
            <TranscriptEditor sourceId={data.source.id} startMs={draft.startMs} endMs={draft.endMs} />
          </Panel>

          <Panel
            title="Publication"
            aside={
              <button
                type="button"
                className="inline-flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground"
                onClick={() =>
                  navigator.clipboard
                    .writeText([draft.title, draft.caption, draft.hashtags].filter(Boolean).join("\n\n"))
                    .then(() => toast.success("Légende copiée"))
                }
              >
                <Copy className="size-3.5" /> Copier
              </button>
            }
          >
            <div className="space-y-3">
              <Field
                label="Légende suggérée"
                hint={clip.social?.origin === "default" ? "générée sans IA" : clip.social?.origin === "test" ? "TEST" : undefined}
              >
                <Textarea rows={2} value={draft.caption} onChange={(e) => draft.set({ caption: e.target.value })} className="border-0 bg-raised" />
              </Field>
              <Field label="Hashtags">
                <Input value={draft.hashtags} onChange={(e) => draft.set({ hashtags: e.target.value })} className="border-0 bg-raised" />
              </Field>
              <p className="text-[11px] text-muted-foreground">
                Publication directe TikTok désactivée : aucune application TikTok Developer n&apos;est configurée. Exportez le MP4, le .srt et cette légende,
                puis publiez depuis TikTok.
              </p>
            </div>
          </Panel>
        </div>
      </div>
    </>
  );
}
