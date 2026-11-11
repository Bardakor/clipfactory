// Formatting and French labels. Pure functions — covered by format.test.ts.

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
  gta_rp: "GTA RP",
  gta_action: "GTA Action",
  general: "Gaming général",
  custom: "Personnalisé",
};

export const PRESET_LABELS: Record<string, string> = { fast: "Rapide", balanced: "Équilibré", max_quality: "Qualité max", custom: "Personnalisé" };

export const LAYOUT_LABELS: Record<string, string> = {
  auto: "Automatique",
  full: "Gameplay complet",
  crop: "Recadrage intelligent",
  split: "Split facecam",
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
