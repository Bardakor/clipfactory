// Formatting and French labels. Pure functions — covered by format.test.ts.

import type { LiveChat, LiveMoment } from "./types";

export function clock(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "—";
  const total = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h > 0 ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
}

/** Timecode with tenths, for trimming. */
export function timecode(ms: number): string {
  const tenths = Math.floor((Math.max(0, ms) % 1000) / 100);
  return `${clock(ms)}.${tenths}`;
}

export function seconds(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "—";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1).replace(".", ",")} s`;
  const m = Math.floor(ms / 60_000);
  const s = Math.round((ms % 60_000) / 1000);
  return m >= 60 ? `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")}` : `${m} min ${String(s).padStart(2, "0")}`;
}

export function bytes(value: number | null | undefined): string {
  if (!value) return "0 o";
  const units = ["o", "Ko", "Mo", "Go", "To"];
  const exponent = Math.min(units.length - 1, Math.floor(Math.log(value) / Math.log(1024)));
  const scaled = value / 1024 ** exponent;
  return `${scaled >= 100 || exponent === 0 ? Math.round(scaled) : scaled.toFixed(1).replace(".", ",")} ${units[exponent]}`;
}

export function usd(value: number | null | undefined): string {
  if (!value) return "0,00 $";
  return `${value < 0.01 ? value.toFixed(4) : value.toFixed(2)} $`.replace(".", ",");
}

export function dateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Intl.DateTimeFormat("fr-FR", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }).format(new Date(iso));
}

/** Local time of day of a timestamp in milliseconds. */
export function timeOfDay(ms: number, withSeconds = false): string {
  return new Intl.DateTimeFormat("fr-FR", { hour: "2-digit", minute: "2-digit", second: withSeconds ? "2-digit" : undefined }).format(new Date(ms));
}

export function elapsedMs(startIso: string | null, endIso: string | null, now: number = Date.now()): number | null {
  if (!startIso) return null;
  return Math.max(0, (endIso ? new Date(endIso).getTime() : now) - new Date(startIso).getTime());
}

export function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

export const STAGE_LABELS: Record<string, string> = {
  import: "Import",
  extract_audio: "Extraction audio",
  transcribe: "Transcription",
  detect_events: "Détection d'événements",
  analyze: "Analyse des scènes candidates",
  rank: "Classement",
  refine: "Affinage des clips",
  render: "Rendu",
  validate: "Validation",
};

export const JOB_STATUS_LABELS: Record<string, string> = {
  queued: "En file",
  running: "En cours",
  succeeded: "Terminé",
  failed: "Échec",
  cancelled: "Annulé",
  none: "—",
};

export const RENDER_STATUS_LABELS: Record<string, string> = {
  none: "Non rendu",
  queued: "En file",
  rendering: "Rendu en cours",
  validating: "Validation",
  rendered: "Rendu validé",
  failed: "Rendu échoué",
  stale: "À re-rendre",
};

export const APPROVAL_LABELS: Record<string, string> = { pending: "À revoir", approved: "Approuvé", rejected: "Rejeté" };

export const PROFILE_LABELS: Record<string, string> = {
  gta6: "GTA 6",
  gta_rp: "GTA RP",
  gta_action: "GTA Action",
  general: "Gaming général",
  custom: "Personnalisé",
};

export const FACECAM_POSITION_LABELS: Record<string, string> = { top: "Streamer en haut", bottom: "Streamer en bas" };

export const PRESET_LABELS: Record<string, string> = { fast: "Rapide", balanced: "Équilibré", max_quality: "Qualité max", custom: "Personnalisé" };

export const LAYOUT_LABELS: Record<string, string> = {
  auto: "Automatique",
  full: "Gameplay complet",
  crop: "Recadrage intelligent",
  split: "Streamer + jeu",
  dynamic: "Dynamique",
  manual: "Manuel",
};

export const STYLE_LABELS: Record<string, string> = { viral: "Viral", clean: "Clean", gaming: "Gaming" };

export const REJECTION_LABELS: Record<string, string> = {
  not_funny: "Pas drôle",
  missing_context: "Contexte manquant",
  too_long: "Trop long",
  wrong_moment: "Mauvais moment",
  bad_subtitles: "Sous-titres ratés",
  bad_crop: "Mauvais cadrage",
  duplicate: "Doublon",
  boring: "Ennuyeux",
};

export const TASK_LABELS: Record<string, string> = {
  transcript_analysis: "Analyse du transcript",
  vision_image: "Compréhension d'images",
  vision_video: "Compréhension vidéo",
  ranking: "Classement éditorial",
  critique: "Critique finale",
  social_caption: "Légendes sociales",
};

export const BASIS_LABELS: Record<string, string> = { ai: "IA", heuristic: "Heuristique", test: "TEST" };

const BUCKET_S = 10;

export type ChatBucket = { at: number; chatters: number | null; spike: boolean };

/** Chat buckets placed on the clock (epoch seconds), flagging those whose spike became a moment. */
export function chatBuckets(chat: LiveChat | null, spikes: number[]): ChatBucket[] {
  if (!chat) return [];
  return chat.chatters.map((chatters, i) => {
    const at = chat.t0 + i * BUCKET_S;
    return { at, chatters, spike: spikes.includes(at) };
  });
}

/** A moment is offered for posting once its pipeline has finished and left a clip; until then it is still under way. */
export function readyToPost(moment: LiveMoment): boolean {
  return moment.clip !== null && moment.job_status === "succeeded";
}

export type Tone = "muted" | "info" | "ok" | "destructive";

/** Where a moment detected in chat stands on its way to a clip. A moment that has its clip is shown as the clip itself. */
export function momentStatus(moment: LiveMoment): { label: string; tone: Tone } {
  if (moment.status === "failed") return { label: moment.detail ?? "Échec", tone: "destructive" };
  if (moment.status === "pending") return { label: "Enregistrement du moment", tone: "info" };
  switch (moment.job_status) {
    case "running":
      return { label: STAGE_LABELS[moment.current_stage ?? ""] ?? "En cours", tone: "info" };
    case "queued":
      return { label: "En file", tone: "muted" };
    case "failed":
      return { label: "Échec du traitement", tone: "destructive" };
    case "cancelled":
      return { label: "Annulé", tone: "muted" };
    case "succeeded":
      return { label: "Aucun clip retenu", tone: "muted" };
    default:
      return { label: "Projet supprimé", tone: "muted" };
  }
}
