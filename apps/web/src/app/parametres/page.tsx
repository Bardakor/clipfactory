"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useQuery } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";
import { useState } from "react";
import { Controller, useForm } from "react-hook-form";
import { z } from "zod";
import { Empty, Field, PageHeader, Segmented } from "@/components/bits";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { api } from "@/lib/api";
import { PRESET_LABELS, PROFILE_LABELS, STYLE_LABELS, TASK_LABELS, bytes } from "@/lib/format";
import { useAction, useHealth, useModels, useStorage } from "@/lib/queries";
import type { ModelsResponse, Preset, RuntimeSettings, SubtitleStyle } from "@/lib/types";

type SettingsPayload = {
  settings: RuntimeSettings;
  profile_weights_default: Record<string, Record<string, number>>;
  required_modality: Record<string, string>;
};

const optionalInt = z.preprocess((v) => (v === "" || v === null || Number.isNaN(v) ? null : Number(v)), z.number().int().min(1).max(16).nullable());
const optionalMoney = z.preprocess((v) => (v === "" || v === null || Number.isNaN(v) ? null : Number(v)), z.number().min(0).nullable());

const schema = z.object({
  preset: z.enum(["fast", "balanced", "max_quality", "custom"]),
  use_video_input: z.boolean(),
  transcription_provider: z.enum(["mlx", "openrouter"]),
  mlx_model: z.string().min(3),
  openrouter_stt_model: z.string().min(3),
  vocabulary_text: z.string(),
  corrections_text: z.string(),
  audio: z.object({
    silence_db: z.number().min(-80).max(-10),
    min_silence_ms: z.number().int().min(100).max(5000),
    peak_z: z.number().min(0.5).max(8),
    group_gap_ms: z.number().int().min(0).max(10000),
  }),
  llm_concurrency: z.number().int().min(1).max(16),
  llm_timeout_s: z.number().int().min(10).max(600),
  llm_max_retries: z.number().int().min(0).max(8),
  render_concurrency: z.number().int().min(1).max(4),
  remotion_concurrency: optionalInt,
  cost_warning_usd: z.number().min(0),
  cost_limit_usd: optionalMoney,
  subtitle_style: z.enum(["viral", "clean", "gaming"]),
  export_dir: z.string(),
});
type FormValues = z.infer<typeof schema>;

const RUBRIC_LABELS: Record<string, string> = {
  hook: "Accroche",
  comprehensibility: "Clarté",
  surprise: "Surprise",
  humor: "Humour",
  emotion: "Émotion",
  narrative: "Récit",
  pacing: "Rythme",
  payoff: "Chute",
  originality: "Originalité",
};

function toForm(s: RuntimeSettings): FormValues {
  return {
    ...s,
    vocabulary_text: s.vocabulary.join(", "),
    corrections_text: Object.entries(s.corrections)
      .map(([from, to]) => `${from} => ${to}`)
      .join("\n"),
    export_dir: s.export_dir ?? "",
  };
}

function Card({ title, description, children }: { title: string; description?: string; children: React.ReactNode }) {
  return (
    <section className="grid gap-x-10 gap-y-4 rounded-xl bg-surface p-6 lg:grid-cols-[16rem_minmax(0,1fr)]">
      <div>
        <h2 className="text-sm font-medium">{title}</h2>
        {description ? <p className="mt-1.5 text-xs leading-relaxed text-muted-foreground">{description}</p> : null}
      </div>
      <div className="min-w-0 space-y-5">{children}</div>
    </section>
  );
}

function ModelRouting({
  models,
  preset,
  custom,
  required,
  onChange,
}: {
  models: ModelsResponse;
  preset: Preset;
  custom: Record<string, string>;
  required: Record<string, string>;
  onChange: (task: string, model: string) => void;
}) {
  const byId = new Map(models.models.map((m) => [m.id, m]));
  return (
    <div className="divide-y divide-border">
      {models.tasks.map((task) => {
        const chain = models.routing[preset]?.[task] ?? [];
        const active = byId.get(custom[task] && byId.get(custom[task])?.input_modalities.includes(required[task]) ? custom[task] : (chain[0] ?? ""));
        const capable = models.models.filter((m) => m.input_modalities.includes(required[task]));
        return (
          <div key={task} className="grid grid-cols-[11rem_minmax(0,1fr)_9rem] items-center gap-4 py-2.5">
            <div>
              <div className="text-[13px]">{TASK_LABELS[task] ?? task}</div>
              <div className="text-[10px] tracking-wide text-muted-foreground uppercase">entrée {required[task]}</div>
            </div>
            <select className="field-select font-mono text-xs" value={custom[task] ?? ""} onChange={(e) => onChange(task, e.target.value)}>
              <option value="">
                Préréglage : {chain[0] ?? "aucun modèle compatible"}
                {chain.length > 1 ? `  (+${chain.length - 1} repli)` : ""}
              </option>
              {capable.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.id}
                </option>
              ))}
            </select>
            <div className="tnum text-right font-mono text-[11px] text-muted-foreground" title="Prix catalogue par million de jetons : entrée / sortie">
              {active ? `${active.prompt_usd_per_mtok.toFixed(2)} / ${active.completion_usd_per_mtok.toFixed(2)} $` : "—"}
            </div>
          </div>
        );
      })}
    </div>
  );
}

export default function SettingsPage() {
  const payload = useQuery({ queryKey: ["settings-full"], queryFn: () => api<SettingsPayload>("/settings") });
  const models = useModels();
  const health = useHealth();
  const storage = useStorage();
  const tiktok = useQuery({ queryKey: ["tiktok"], queryFn: () => api<{ enabled: boolean; reason: string; requirements: string[] }>("/publishing/tiktok") });
  const [customModels, setCustomModels] = useState<Record<string, string> | null>(null);
  const [weights, setWeights] = useState<Record<string, Record<string, number>> | null>(null);
  const [profile, setProfile] = useState("gta_rp");

  const saved = payload.data?.settings;
  const form = useForm<FormValues>({
    resolver: zodResolver(schema) as never,
    values: saved ? toForm(saved) : undefined,
    resetOptions: { keepDirtyValues: true },
  });
  const { register, control, watch, handleSubmit, formState } = form;

  const save = useAction((body: RuntimeSettings) => api("/settings", { method: "PUT", body }), {
    success: "Paramètres enregistrés",
    onDone: () => {
      setCustomModels(null);
      setWeights(null);
    },
  });
  const refresh = useAction(() => api("/models?refresh=true"), { success: "Catalogue OpenRouter rafraîchi" });
  const cleanup = useAction((targets: string[]) => api<{ freed_bytes: number }>("/storage/cleanup", { method: "POST", body: { targets } }), {
    success: "Espace libéré",
  });

  if (payload.isLoading) return <Skeleton className="h-96 rounded-xl" />;
  if (!saved || !payload.data) return <Empty title="Impossible de charger les paramètres">{payload.error?.message}</Empty>;

  const routed = customModels ?? saved.custom_models;
  const profileWeights = weights ?? saved.profile_weights;
  const defaults = payload.data.profile_weights_default[profile] ?? {};
  const preset = watch("preset") ?? saved.preset;
  const pendingExtra = customModels !== null || weights !== null;

  const submit = handleSubmit((values) => {
    const { vocabulary_text, corrections_text, export_dir, ...rest } = values;
    const corrections = Object.fromEntries(
      corrections_text
        .split("\n")
        .map((line) => line.split("=>").map((part) => part.trim()))
        .filter((parts) => parts.length === 2 && parts[0] && parts[1]),
    );
    save.mutate({
      ...saved,
      ...rest,
      vocabulary: vocabulary_text
        .split(",")
        .map((w) => w.trim())
        .filter(Boolean),
      corrections,
      export_dir: export_dir.trim() || null,
      custom_models: Object.fromEntries(Object.entries(routed).filter(([, model]) => model)),
      profile_weights: profileWeights,
    });
  });

  const number = (name: Parameters<typeof register>[0]) => register(name, { valueAsNumber: true });

  return (
    <form onSubmit={submit}>
      <PageHeader
        title="Paramètres"
        description="Réglages appliqués aux prochains traitements. Les secrets restent dans le fichier .env du serveur."
        actions={
          <Button type="submit" disabled={save.isPending || (!formState.isDirty && !pendingExtra)}>
            Enregistrer
          </Button>
        }
      />
      {Object.keys(formState.errors).length > 0 ? (
        <p className="mb-4 text-xs text-destructive">Certaines valeurs sont hors limites : {Object.keys(formState.errors).join(", ")}</p>
      ) : null}

      <div className="space-y-5">
        <Card title="Clé OpenRouter" description="Seul identifiant externe requis. Elle n'est jamais envoyée au navigateur ni écrite dans les journaux.">
          <div className="flex items-start gap-3 text-sm">
            <span className={`mt-1.5 size-2 rounded-full ${health.data?.openrouter_key_configured ? "bg-ok" : "bg-warn"}`} />
            {health.data?.openrouter_key_configured ? (
              <span>Clé détectée côté serveur.</span>
            ) : (
              <span>
                Aucune clé. Ajoutez <code className="rounded bg-raised px-1.5 py-0.5 font-mono text-xs">OPENROUTER_API_KEY=…</code> dans le fichier{" "}
                <code className="rounded bg-raised px-1.5 py-0.5 font-mono text-xs">.env</code> à la racine du dépôt, puis relancez l&apos;API et le worker.
                <span className="mt-1 block text-xs text-muted-foreground">
                  Sans clé, le pipeline fonctionne mais le classement est uniquement heuristique, et signalé comme tel.
                </span>
              </span>
            )}
          </div>
        </Card>

        <Card
          title="Routage des modèles"
          description="Chaque tâche a son modèle. Les préférences sont vérifiées contre le catalogue OpenRouter : un modèle absent ou sans la modalité requise est ignoré."
        >
          <div className="flex flex-wrap items-center justify-between gap-3">
            <Controller
              control={control}
              name="preset"
              render={({ field }) => (
                <Segmented
                  value={field.value ?? saved.preset}
                  onChange={field.onChange}
                  options={Object.entries(PRESET_LABELS).map(([value, label]) => ({ value: value as Preset, label }))}
                />
              )}
            />
            <Button type="button" variant="ghost" size="sm" disabled={refresh.isPending} onClick={() => refresh.mutate(undefined)}>
              <RefreshCw className={`size-3.5 ${refresh.isPending ? "animate-spin" : ""}`} /> Rafraîchir le catalogue
              {models.data?.fetched_at ? (
                <span className="text-muted-foreground">· {new Date(models.data.fetched_at * 1000).toLocaleDateString("fr-FR")}</span>
              ) : null}
            </Button>
          </div>
          {models.data && models.data.models.length > 0 ? (
            <ModelRouting
              models={models.data}
              preset={preset}
              custom={routed}
              required={payload.data.required_modality}
              onChange={(task, model) => setCustomModels({ ...routed, [task]: model })}
            />
          ) : (
            <p className="text-sm text-muted-foreground">
              {models.isLoading
                ? "Chargement du catalogue…"
                : "Catalogue OpenRouter indisponible (hors ligne ?). Les préférences du préréglage seront utilisées telles quelles."}
            </p>
          )}
          <div className="flex items-center justify-between">
            <div>
              <div className="text-xs font-medium">Envoyer de courts extraits vidéo quand le modèle les accepte</div>
              <div className="text-[11px] text-muted-foreground">Uniquement en « Qualité max ». Sinon, ou en cas d&apos;échec : images clés horodatées.</div>
            </div>
            <Controller
              control={control}
              name="use_video_input"
              render={({ field }) => <Switch checked={field.value ?? false} onCheckedChange={field.onChange} />}
            />
          </div>
        </Card>

        <Card title="Transcription" description="MLX Whisper tourne en local sur Apple Silicon, sans coût. OpenRouter sert de solution de repli.">
          <Controller
            control={control}
            name="transcription_provider"
            render={({ field }) => (
              <Segmented
                value={field.value ?? "mlx"}
                onChange={field.onChange}
                options={[
                  { value: "mlx", label: "MLX Whisper (local)" },
                  { value: "openrouter", label: "OpenRouter" },
                ]}
              />
            )}
          />
          <div className="grid grid-cols-2 gap-4">
            <Field label="Modèle MLX">
              <Input className="border-0 bg-raised font-mono text-xs" {...register("mlx_model")} />
            </Field>
            <Field label="Modèle OpenRouter">
              <Input className="border-0 bg-raised font-mono text-xs" {...register("openrouter_stt_model")} />
            </Field>
          </div>
          <Field label="Vocabulaire gaming" hint="séparé par des virgules">
            <Textarea rows={2} className="border-0 bg-raised text-[13px]" {...register("vocabulary_text")} />
          </Field>
          <Field label="Dictionnaire de corrections" hint="une règle par ligne : entendu => correct">
            <Textarea rows={5} className="border-0 bg-raised font-mono text-xs" {...register("corrections_text")} />
          </Field>
        </Card>

        <Card
          title="Détecteur audio"
          description="Les événements sont mesurés par contraste avec les 30 secondes précédentes : un volume élevé et constant ne compte pas."
        >
          <div className="grid grid-cols-2 gap-4 xl:grid-cols-4">
            <Field label="Seuil de silence (dB)">
              <Input type="number" step="1" className="border-0 bg-raised" {...number("audio.silence_db")} />
            </Field>
            <Field label="Silence minimal (ms)">
              <Input type="number" className="border-0 bg-raised" {...number("audio.min_silence_ms")} />
            </Field>
            <Field label="Sensibilité (z-score)">
              <Input type="number" step="0.1" className="border-0 bg-raised" {...number("audio.peak_z")} />
            </Field>
            <Field label="Regroupement (ms)">
              <Input type="number" className="border-0 bg-raised" {...number("audio.group_gap_ms")} />
            </Field>
          </div>
        </Card>

        <Card
          title="Performance et budget"
          description="Les rendus Remotion consomment plusieurs Go : gardez un seul rendu simultané sur une machine à mémoire unifiée modeste."
        >
          <div className="grid grid-cols-2 gap-4 xl:grid-cols-3">
            <Field label="Requêtes IA simultanées">
              <Input type="number" className="border-0 bg-raised" {...number("llm_concurrency")} />
            </Field>
            <Field label="Délai par requête (s)">
              <Input type="number" className="border-0 bg-raised" {...number("llm_timeout_s")} />
            </Field>
            <Field label="Nouvelles tentatives">
              <Input type="number" className="border-0 bg-raised" {...number("llm_max_retries")} />
            </Field>
            <Field label="Rendus simultanés">
              <Input type="number" className="border-0 bg-raised" {...number("render_concurrency")} />
            </Field>
            <Field label="Onglets Remotion" hint="vide = automatique">
              <Input type="number" className="border-0 bg-raised" {...register("remotion_concurrency")} />
            </Field>
            <div />
            <Field label="Alerte de coût par job ($)">
              <Input type="number" step="0.1" className="border-0 bg-raised" {...number("cost_warning_usd")} />
            </Field>
            <Field label="Plafond dur par job ($)" hint="vide = aucun">
              <Input type="number" step="0.1" className="border-0 bg-raised" {...register("cost_limit_usd")} />
            </Field>
          </div>
        </Card>

        <Card title="Sous-titres par défaut" description="Style proposé à la création d'un projet. Chaque clip reste modifiable dans l'éditeur.">
          <Controller
            control={control}
            name="subtitle_style"
            render={({ field }) => (
              <Segmented
                value={field.value ?? "viral"}
                onChange={field.onChange}
                options={Object.entries(STYLE_LABELS).map(([value, label]) => ({ value: value as SubtitleStyle, label }))}
              />
            )}
          />
        </Card>

        <Card
          title="Poids des profils"
          description="Importance relative de chaque critère de la grille éditoriale. Les poids sont normalisés : seul leur rapport compte."
        >
          <Segmented value={profile} onChange={setProfile} options={Object.entries(PROFILE_LABELS).map(([value, label]) => ({ value, label }))} />
          <div className="grid gap-x-8 gap-y-3 sm:grid-cols-2 xl:grid-cols-3">
            {Object.entries(RUBRIC_LABELS).map(([key, label]) => {
              const value = profileWeights[profile]?.[key] ?? defaults[key] ?? 1;
              return (
                <Field
                  key={key}
                  label={label}
                  hint={
                    <span className="tnum">
                      {value.toFixed(1)}
                      {profileWeights[profile]?.[key] !== undefined ? " · modifié" : ""}
                    </span>
                  }
                >
                  <input
                    type="range"
                    className="field-range"
                    min={0}
                    max={3}
                    step={0.1}
                    value={value}
                    onChange={(e) => setWeights({ ...profileWeights, [profile]: { ...(profileWeights[profile] ?? {}), [key]: Number(e.target.value) } })}
                  />
                </Field>
              );
            })}
          </div>
          {profileWeights[profile] ? (
            <button
              type="button"
              className="text-xs text-muted-foreground hover:text-foreground"
              onClick={() => {
                const next = { ...profileWeights };
                delete next[profile];
                setWeights(next);
              }}
            >
              Rétablir les poids par défaut de ce profil
            </button>
          ) : null}
        </Card>

        <Card
          title="Stockage"
          description="Les vidéos sources et les exports ne sont jamais supprimés ici : utilisez la suppression d'une source ou d'un projet."
        >
          {storage.data ? (
            <>
              <div className="font-mono text-[11px] text-muted-foreground">{storage.data.data_dir}</div>
              <dl className="grid grid-cols-2 gap-x-8 gap-y-1.5 text-[13px] sm:grid-cols-4">
                {Object.entries(storage.data.bytes).map(([name, size]) => (
                  <div key={name} className="flex justify-between gap-3">
                    <dt className="text-muted-foreground">{name}</dt>
                    <dd className="tnum">{bytes(size)}</dd>
                  </div>
                ))}
              </dl>
              <div className="flex flex-wrap gap-2">
                {(
                  [
                    ["work", "Fichiers de travail"],
                    ["previews", "Aperçus"],
                    ["frames", "Images clés"],
                    ["llm", "Réponses IA en cache"],
                  ] as const
                ).map(([target, label]) => (
                  <Button
                    key={target}
                    type="button"
                    variant="secondary"
                    size="sm"
                    disabled={cleanup.isPending}
                    onClick={() => {
                      if (target !== "llm" || window.confirm("Vider ce cache obligera à repayer les mêmes analyses. Continuer ?")) cleanup.mutate([target]);
                    }}
                  >
                    Vider : {label}
                  </Button>
                ))}
              </div>
            </>
          ) : (
            <Skeleton className="h-16" />
          )}
          <Field label="Copier aussi les exports finaux vers" hint="chemin absolu, facultatif">
            <Input placeholder="/Users/moi/Movies/Clips" className="border-0 bg-raised font-mono text-xs" {...register("export_dir")} />
          </Field>
        </Card>

        <Card title="Publication TikTok" description="Flux « export d'abord » : chaque clip se télécharge avec sa légende, ses hashtags et ses sous-titres.">
          <div className="flex items-start gap-3 text-sm">
            <span className="mt-1.5 size-2 rounded-full bg-muted-foreground/50" />
            <div>
              <div>Désactivée</div>
              <p className="mt-1 text-xs text-muted-foreground">{tiktok.data?.reason}</p>
              <ul className="mt-2 list-inside list-disc text-xs text-muted-foreground">
                {tiktok.data?.requirements.map((r) => (
                  <li key={r}>{r}</li>
                ))}
              </ul>
            </div>
          </div>
        </Card>
      </div>
    </form>
  );
}
