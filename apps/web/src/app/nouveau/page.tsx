"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { FileVideo, Link2, Loader2, UploadCloud, X } from "lucide-react";
import { useRouter } from "next/navigation";
import { useRef, useState } from "react";
import { Controller, useForm } from "react-hook-form";
import { toast } from "sonner";
import { z } from "zod";
import { ErrorNote, Field, PageHeader, SectionTitle, Segmented } from "@/components/bits";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { api, uploadSource } from "@/lib/api";
import { LAYOUT_LABELS, PRESET_LABELS, PROFILE_LABELS, STYLE_LABELS, bytes, clock } from "@/lib/format";
import { useHealth, useRuntimeSettings } from "@/lib/queries";
import type { Project, Source, YouTubeInfo } from "@/lib/types";
import { cn } from "@/lib/utils";

const schema = z
  .object({
    profile: z.enum(["gta_rp", "gta_action", "general", "custom"]),
    clip_count: z.number().int().min(1).max(30),
    min_len_s: z.number().int().min(5).max(120),
    max_len_s: z.number().int().min(10).max(180),
    quality_threshold: z.number().int().min(0).max(100),
    preset: z.enum(["fast", "balanced", "max_quality", "custom"]),
    subtitle_style: z.enum(["viral", "clean", "gaming"]),
    burn_subtitles: z.boolean(),
    layout: z.enum(["auto", "full", "crop", "split", "dynamic"]),
    resolution: z.enum(["1080x1920", "720x1280"]),
    fps: z.enum(["30", "60"]),
    language: z.enum(["auto", "fr", "en"]),
    instructions: z.string().max(1000),
  })
  .refine((v) => v.max_len_s > v.min_len_s, { path: ["max_len_s"], message: "La durée max doit dépasser la durée min" });

type FormValues = z.infer<typeof schema>;

const EXAMPLES = [
  "Priorise les interactions drôles avec la police.",
  "Ignore les fusillades répétitives.",
  "Trouve les moments où le streamer rit sans pouvoir s'arrêter.",
  "Privilégie les conversations drôles plutôt que les courses-poursuites.",
];

const ACCEPTED = [".mp4", ".mov", ".mkv", ".webm", ".m4v"];

export default function NewProjectPage() {
  const router = useRouter();
  const health = useHealth();
  const runtime = useRuntimeSettings();
  const [mode, setMode] = useState<"youtube" | "upload">("youtube");
  const [url, setUrl] = useState("");
  const [video, setVideo] = useState<YouTubeInfo | null>(null);
  const [rights, setRights] = useState(false);
  const [inspecting, setInspecting] = useState(false);
  const [source, setSource] = useState<Source | null>(null);
  const [upload, setUpload] = useState<{ name: string; fraction: number; abort: () => void } | null>(null);
  const [dragging, setDragging] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  const form = useForm<FormValues>({
    resolver: zodResolver(schema),
    values: {
      profile: "gta_rp",
      clip_count: 8,
      min_len_s: 15,
      max_len_s: 90,
      quality_threshold: 45,
      preset: runtime.data?.preset ?? "balanced",
      subtitle_style: runtime.data?.subtitle_style ?? "viral",
      burn_subtitles: true,
      layout: "auto",
      resolution: "1080x1920",
      fps: "30",
      language: "auto",
      instructions: "",
    },
    resetOptions: { keepDirtyValues: true },
  });
  const { register, control, watch, setValue, handleSubmit, formState } = form;

  async function inspect() {
    setProblem(null);
    setVideo(null);
    setInspecting(true);
    try {
      setVideo(await api<YouTubeInfo>("/sources/youtube/inspect", { method: "POST", body: { url } }));
    } catch (error) {
      setProblem((error as Error).message);
    } finally {
      setInspecting(false);
    }
  }

  function startUpload(file: File) {
    setProblem(null);
    setSource(null);
    if (!ACCEPTED.some((ext) => file.name.toLowerCase().endsWith(ext))) {
      setProblem(`Format non pris en charge. Formats acceptés : ${ACCEPTED.join(", ")}`);
      return;
    }
    const { promise, abort } = uploadSource<{ source: Source; duplicate: boolean }>(file, (fraction) => setUpload((u) => (u ? { ...u, fraction } : u)));
    setUpload({ name: file.name, fraction: 0, abort });
    promise
      .then((result) => {
        setSource(result.source);
        if (result.duplicate) toast.info("Cette vidéo a déjà été importée : ses analyses seront réutilisées.");
      })
      .catch((error: Error) => setProblem(error.message))
      .finally(() => setUpload(null));
  }

  const submit = handleSubmit(async (values) => {
    setProblem(null);
    const [width, height] = values.resolution.split("x").map(Number);
    const settings = {
      profile: values.profile,
      clip_count: values.clip_count,
      min_len_s: values.min_len_s,
      max_len_s: values.max_len_s,
      quality_threshold: values.quality_threshold,
      preset: values.preset,
      subtitle_style: values.subtitle_style,
      burn_subtitles: values.burn_subtitles,
      layout: values.layout,
      language: values.language,
      instructions: values.instructions,
      width,
      height,
      fps: Number(values.fps),
      subtitle_options: runtime.data?.subtitle_options,
    };
    try {
      const body = mode === "upload" ? { source_id: source?.id, settings } : { youtube_url: video?.url, rights_confirmed: rights, settings };
      const { project } = await api<{ project: Project }>("/projects", { method: "POST", body });
      router.push(`/projets/${project.id}`);
    } catch (error) {
      setProblem((error as Error).message);
    }
  });

  const ready = mode === "upload" ? Boolean(source) : Boolean(video) && rights;
  const count = watch("clip_count");
  const threshold = watch("quality_threshold");

  return (
    <form onSubmit={submit}>
      <PageHeader title="Nouveau projet" description="Une vidéo longue en entrée, plusieurs clips verticaux sous-titrés en sortie." />

      <section className="rounded-xl bg-surface p-6">
        <div className="mb-5 flex items-center justify-between">
          <SectionTitle>Source</SectionTitle>
          <Segmented
            value={mode}
            onChange={setMode}
            options={[
              { value: "youtube", label: "URL YouTube" },
              { value: "upload", label: "Fichier local" },
            ]}
          />
        </div>

        {mode === "youtube" ? (
          <div>
            <div className="flex gap-3">
              <div className="relative flex-1">
                <Link2 className="absolute top-1/2 left-4 size-5 -translate-y-1/2 text-muted-foreground" />
                <Input
                  value={url}
                  onChange={(e) => {
                    setUrl(e.target.value);
                    setVideo(null);
                    setRights(false);
                  }}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") {
                      e.preventDefault();
                      if (url.trim()) void inspect();
                    }
                  }}
                  placeholder="https://www.youtube.com/watch?v=…"
                  aria-label="URL YouTube"
                  className="h-14 border-0 bg-raised pl-12 text-base"
                />
              </div>
              <Button type="button" variant="secondary" className="h-14 px-6" disabled={!url.trim() || inspecting} onClick={inspect}>
                {inspecting ? <Loader2 className="size-4 animate-spin" /> : "Vérifier"}
              </Button>
            </div>
            {video ? (
              <div className="mt-5 flex gap-5">
                {video.thumbnail_url ? (
                  // eslint-disable-next-line @next/next/no-img-element -- remote thumbnail, shown as-is
                  <img src={video.thumbnail_url} alt="" className="aspect-video w-56 shrink-0 rounded-lg object-cover" />
                ) : null}
                <div className="min-w-0 flex-1">
                  <div className="line-clamp-2 font-medium">{video.title}</div>
                  <div className="mt-1 text-sm text-muted-foreground">
                    {video.channel ?? "Chaîne inconnue"} · {video.duration_s ? clock(video.duration_s * 1000) : "durée inconnue"}
                  </div>
                  <label className="mt-4 flex cursor-pointer items-start gap-3 rounded-lg bg-raised p-3 text-[13px] leading-snug">
                    <input type="checkbox" checked={rights} onChange={(e) => setRights(e.target.checked)} className="mt-0.5 size-4 accent-[var(--primary)]" />
                    <span>
                      Je confirme être propriétaire de cette vidéo ou autorisé(e) à la télécharger et à la réutiliser.
                      <span className="mt-0.5 block text-muted-foreground">
                        Seuls les formats publics sont téléchargés ; aucune protection n&apos;est contournée.
                      </span>
                    </span>
                  </label>
                </div>
              </div>
            ) : null}
          </div>
        ) : (
          <div>
            <input
              ref={fileInput}
              type="file"
              accept={ACCEPTED.join(",")}
              className="hidden"
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) startUpload(f);
                e.target.value = "";
              }}
            />
            {source ? (
              <div className="flex items-center gap-4 rounded-lg bg-raised p-4">
                <FileVideo className="size-8 shrink-0 text-primary" strokeWidth={1.5} />
                <div className="min-w-0 flex-1">
                  <div className="truncate font-medium">{source.title}</div>
                  <div className="tnum mt-0.5 text-xs text-muted-foreground">
                    {clock(source.duration_ms)} · {source.width}×{source.height} · {source.fps} i/s · {source.video_codec}/{source.audio_codec ?? "sans audio"}{" "}
                    · {bytes(source.bytes)}
                  </div>
                </div>
                <Button type="button" variant="ghost" size="icon" onClick={() => setSource(null)} aria-label="Retirer la source">
                  <X className="size-4" />
                </Button>
              </div>
            ) : upload ? (
              <div className="rounded-lg bg-raised p-5">
                <div className="mb-2 flex items-center justify-between text-sm">
                  <span className="truncate">{upload.name}</span>
                  <span className="tnum text-muted-foreground">{upload.fraction < 1 ? `${Math.round(upload.fraction * 100)} %` : "analyse du fichier…"}</span>
                </div>
                <div className="h-1 overflow-hidden rounded-full bg-background">
                  <div className="h-full bg-primary transition-[width]" style={{ width: `${upload.fraction * 100}%` }} />
                </div>
                <button type="button" className="mt-3 text-xs text-muted-foreground hover:text-foreground" onClick={upload.abort}>
                  Annuler l&apos;envoi
                </button>
              </div>
            ) : (
              <button
                type="button"
                onClick={() => fileInput.current?.click()}
                onDragOver={(e) => {
                  e.preventDefault();
                  setDragging(true);
                }}
                onDragLeave={() => setDragging(false)}
                onDrop={(e) => {
                  e.preventDefault();
                  setDragging(false);
                  const f = e.dataTransfer.files?.[0];
                  if (f) startUpload(f);
                }}
                className={cn(
                  "flex w-full flex-col items-center gap-2 rounded-lg border border-dashed px-6 py-12 transition-colors",
                  dragging ? "border-primary bg-primary/5" : "border-input hover:border-muted-foreground/50",
                )}
              >
                <UploadCloud className="size-7 text-muted-foreground" strokeWidth={1.5} />
                <span className="text-sm font-medium">Déposez une vidéo ici, ou cliquez pour choisir</span>
                <span className="text-xs text-muted-foreground">MP4, MOV, MKV, WebM</span>
              </button>
            )}
          </div>
        )}
        {problem ? (
          <div className="mt-5">
            <ErrorNote title="Impossible de continuer">{problem}</ErrorNote>
          </div>
        ) : null}
      </section>

      <section className="mt-6 grid gap-x-10 gap-y-7 rounded-xl bg-surface p-6 lg:grid-cols-2">
        <div className="space-y-6">
          <SectionTitle>Sélection éditoriale</SectionTitle>
          <Field group label="Profil gaming">
            <Controller
              control={control}
              name="profile"
              render={({ field }) => (
                <Segmented
                  className="w-full"
                  value={field.value}
                  onChange={field.onChange}
                  options={Object.entries(PROFILE_LABELS).map(([value, label]) => ({ value: value as FormValues["profile"], label }))}
                />
              )}
            />
          </Field>
          <Field label="Nombre de clips souhaité" hint={<span className="tnum">{count}</span>}>
            <input type="range" min={1} max={30} className="field-range" {...register("clip_count", { valueAsNumber: true })} />
          </Field>
          <div className="grid grid-cols-2 gap-4">
            <Field label="Durée min (s)">
              <Input type="number" className="border-0 bg-raised" {...register("min_len_s", { valueAsNumber: true })} />
            </Field>
            <Field label="Durée max (s)">
              <Input type="number" className="border-0 bg-raised" {...register("max_len_s", { valueAsNumber: true })} />
            </Field>
          </div>
          {formState.errors.max_len_s ? <p className="-mt-3 text-xs text-destructive">{formState.errors.max_len_s.message}</p> : null}
          <Field label="Seuil de qualité" hint={<span className="tnum">{threshold} / 100</span>}>
            <input type="range" min={0} max={100} className="field-range" {...register("quality_threshold", { valueAsNumber: true })} />
            <span className="mt-1.5 block text-[11px] text-muted-foreground">
              Moins de clips que demandé seront produits si la source n&apos;en contient pas assez au-dessus de ce seuil.
            </span>
          </Field>
          <Field
            group
            label="Préréglage d'inférence IA"
            hint={health.data && !health.data.openrouter_key_configured ? <span className="text-warn">clé OpenRouter absente</span> : undefined}
          >
            <Controller
              control={control}
              name="preset"
              render={({ field }) => (
                <Segmented
                  className="w-full"
                  value={field.value}
                  onChange={field.onChange}
                  options={Object.entries(PRESET_LABELS).map(([value, label]) => ({ value: value as FormValues["preset"], label }))}
                />
              )}
            />
          </Field>
          <Field label="Consignes éditoriales (facultatif)">
            <Textarea
              rows={3}
              placeholder="Ex. : privilégie les dialogues absurdes entre policiers et civils."
              className="border-0 bg-raised"
              {...register("instructions")}
            />
            <div className="mt-2 flex flex-wrap gap-1.5">
              {EXAMPLES.map((example) => (
                <button
                  key={example}
                  type="button"
                  onClick={() => setValue("instructions", example, { shouldDirty: true })}
                  className="rounded-full bg-raised px-2.5 py-1 text-[11px] text-muted-foreground hover:text-foreground"
                >
                  {example}
                </button>
              ))}
            </div>
          </Field>
        </div>

        <div className="space-y-6">
          <SectionTitle>Rendu</SectionTitle>
          <Field group label="Style de sous-titres">
            <Controller
              control={control}
              name="subtitle_style"
              render={({ field }) => (
                <Segmented
                  className="w-full"
                  value={field.value}
                  onChange={field.onChange}
                  options={Object.entries(STYLE_LABELS).map(([value, label]) => ({ value: value as FormValues["subtitle_style"], label }))}
                />
              )}
            />
          </Field>
          <div className="flex items-center justify-between">
            <div>
              <div className="text-xs font-medium text-foreground/85">Incruster les sous-titres</div>
              <div className="text-[11px] text-muted-foreground">Le fichier .srt est exporté dans tous les cas.</div>
            </div>
            <Controller control={control} name="burn_subtitles" render={({ field }) => <Switch checked={field.value} onCheckedChange={field.onChange} />} />
          </div>
          <Field label="Mise en page verticale">
            <select className="field-select" {...register("layout")}>
              {(["auto", "full", "crop", "split", "dynamic"] as const).map((value) => (
                <option key={value} value={value}>
                  {LAYOUT_LABELS[value]}
                </option>
              ))}
            </select>
            <span className="mt-1.5 block text-[11px] text-muted-foreground">
              « Automatique » garde tout le gameplay visible, et passe en split si une facecam est détectée de façon fiable.
            </span>
          </Field>
          <div className="grid grid-cols-3 gap-4">
            <Field label="Résolution">
              <select className="field-select" {...register("resolution")}>
                <option value="1080x1920">1080×1920</option>
                <option value="720x1280">720×1280</option>
              </select>
            </Field>
            <Field label="Images/s">
              <select className="field-select" {...register("fps")}>
                <option value="30">30</option>
                <option value="60">60</option>
              </select>
            </Field>
            <Field label="Langue">
              <select className="field-select" {...register("language")}>
                <option value="auto">Auto</option>
                <option value="fr">Français</option>
                <option value="en">Anglais</option>
              </select>
            </Field>
          </div>
        </div>
      </section>

      <div className="mt-6 flex items-center justify-end gap-4">
        {!ready ? (
          <span className="text-xs text-muted-foreground">
            {mode === "upload" ? "Ajoutez une vidéo pour continuer." : video ? "Confirmez vos droits sur la vidéo." : "Vérifiez d'abord l'URL."}
          </span>
        ) : null}
        <Button type="submit" size="lg" className="h-11 px-7" disabled={!ready || formState.isSubmitting}>
          {formState.isSubmitting ? <Loader2 className="size-4 animate-spin" /> : null}
          Générer les clips
        </Button>
      </div>
    </form>
  );
}
