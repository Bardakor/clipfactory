// Shapes returned by the CLIPFACTORY API. Kept by hand next to the Python serializers.

export type ApiErrorBody = { code: string; message: string; retryable: boolean; details: Record<string, unknown> };

export type Profile = "gta6" | "gta_rp" | "gta_action" | "general" | "custom";
export type Preset = "fast" | "balanced" | "max_quality" | "custom";
export type SubtitleStyle = "viral" | "clean" | "gaming";
export type Layout = "auto" | "full" | "crop" | "split" | "dynamic" | "manual";
export type JobStatus = "queued" | "running" | "succeeded" | "failed" | "cancelled";
export type Approval = "pending" | "approved" | "rejected";

export type SubtitleOptions = {
  font: string | null;
  scale: number;
  accent: string | null;
  position: "auto" | "top" | "middle" | "bottom";
  emphasis: boolean;
};

export type ProjectSettings = {
  profile: Profile;
  clip_count: number;
  min_len_s: number;
  max_len_s: number;
  quality_threshold: number;
  preset: Preset;
  subtitle_style: SubtitleStyle;
  subtitle_options: SubtitleOptions;
  burn_subtitles: boolean;
  layout: Layout;
  width: number;
  height: number;
  fps: 30 | 60;
  language: "auto" | "fr" | "en";
  instructions: string;
};

export type Source = {
  id: string;
  kind: "upload" | "youtube";
  status: string;
  title: string;
  channel: string | null;
  origin_url: string | null;
  original_filename: string | null;
  bytes: number | null;
  duration_ms: number | null;
  fps: number | null;
  width: number | null;
  height: number | null;
  video_codec: string | null;
  audio_codec: string | null;
  audio_channels: number | null;
  thumbnail_url: string | null;
  created_at: string;
  projects?: number;
};

export type Stage = {
  name: string;
  status: "pending" | "running" | "succeeded" | "failed" | "cancelled" | "skipped";
  progress: number | null;
  detail: string | null;
  cache_hit: boolean;
  metrics: Record<string, unknown> | null;
  error: ApiErrorBody | null;
  started_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
};

export type Job = {
  id: string;
  project_id: string;
  clip_id: string | null;
  kind: "pipeline" | "render";
  status: JobStatus;
  attempt: number;
  cancel_requested: boolean;
  error: (ApiErrorBody & { stage?: string }) | null;
  warnings: string[];
  is_test: boolean;
  payload: { clip_id?: string; mode?: "preview" | "final" } | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  stages: Stage[];
  cost: { actual_usd: number; estimated_usd: number; requests: number };
};

export type Clip = {
  id: string;
  project_id: string;
  project_name?: string;
  rank: number;
  start_ms: number;
  end_ms: number;
  original_start_ms: number;
  original_end_ms: number;
  duration_ms: number;
  score: number;
  score_basis: "ai" | "heuristic" | "test";
  title: string;
  main_event: string;
  justification: string;
  layout: Layout;
  layout_params: { manual?: { x: number; y: number; w: number; h: number } } | null;
  resolved_layout: { used: string; reason: string } | null;
  subtitle_style: SubtitleStyle;
  subtitle_options: Partial<SubtitleOptions> | null;
  captions_precision: "word" | "segment";
  captions_edited: boolean;
  social: { title?: string; caption?: string; hashtags?: string[]; origin?: string } | null;
  render_status: "none" | "queued" | "rendering" | "validating" | "rendered" | "failed" | "stale";
  approval: Approval;
  edited: boolean;
  is_test: boolean;
  validation: { ok: boolean; checks?: Record<string, boolean>; failures?: string[]; details?: Record<string, unknown>; error?: ApiErrorBody } | null;
  video_url: string | null;
  preview_url: string | null;
  has_export: boolean;
  has_srt: boolean;
  created_at: string;
  updated_at: string;
};

export type CaptionPage = { index: number; text: string; start_ms: number; end_ms: number; measured: boolean };

export type ClipDetail = {
  clip: Clip & {
    caption_pages: CaptionPage[];
    feedback: Array<{ decision: string; rejection_reason: string | null; rating: number | null; created_at: string }>;
  };
  source: { id: string; title: string; duration_ms: number; width: number; height: number; origin_url: string | null; facecam_found: boolean };
  project: { id: string; name: string; settings: ProjectSettings };
  evidence: {
    features?: Record<string, number>;
    components?: Record<string, number>;
    rubric?: Record<string, number> | null;
    visual?: Record<string, unknown>;
    refinement?: string[];
  } | null;
  penalties: Record<string, number> | null;
  active_job: Job | null;
};

export type Candidate = {
  id: string;
  start_ms: number;
  end_ms: number;
  origins: string[];
  title: string;
  event_type: string;
  final_score: number | null;
  heuristic_score: number | null;
  status: "pool" | "selected" | "rejected";
  reject_reason: string | null;
  justification: string;
};

export type Project = {
  id: string;
  name: string;
  settings: ProjectSettings;
  summary: { explanation?: string; evaluated?: number; selected?: number; requested?: number; basis?: string } | null;
  is_test: boolean;
  created_at: string;
  source: Source;
  status: JobStatus | "none";
  job_id: string | null;
  clip_count: number;
  current_stage: string | null;
  job?: Job | null;
  clips?: Clip[];
  render_jobs?: Job[];
  candidates?: Candidate[];
};

export type Health = {
  ok: boolean;
  dependencies: Record<string, string | null>;
  worker_alive: boolean;
  openrouter_key_configured: boolean;
  test_mode: boolean;
  ai_adapter: string;
  transcriber_override: string | null;
};

export type Stats = {
  sources_processed: number;
  clips_rendered: number;
  clips_awaiting_review: number;
  clips_approved: number;
  jobs_succeeded: number;
  jobs_failed: number;
  jobs_active: number;
  inference: { actual_usd: number; estimated_usd: number; requests: number };
  avg_processing_s: number | null;
  avg_realtime_factor: number | null;
  test_projects: number;
  history: Array<{ project_id: string; name: string; is_test: boolean; created_at: string; status: JobStatus; processing_s: number | null }>;
};

export type RuntimeSettings = {
  preset: Preset;
  custom_models: Record<string, string>;
  use_video_input: boolean;
  transcription_provider: "mlx" | "openrouter";
  mlx_model: string;
  openrouter_stt_model: string;
  vocabulary: string[];
  corrections: Record<string, string>;
  audio: { silence_db: number; min_silence_ms: number; peak_z: number; group_gap_ms: number };
  llm_concurrency: number;
  llm_timeout_s: number;
  llm_max_retries: number;
  render_concurrency: number;
  remotion_concurrency: number | null;
  stage_timeout_s: number;
  cost_warning_usd: number;
  cost_limit_usd: number | null;
  subtitle_style: SubtitleStyle;
  subtitle_options: SubtitleOptions;
  profile_weights: Record<string, Record<string, number>>;
  export_dir: string | null;
};

export type ModelInfo = {
  id: string;
  name: string;
  input_modalities: string[];
  structured_outputs: boolean;
  context_length: number;
  prompt_usd_per_mtok: number;
  completion_usd_per_mtok: number;
};

export type ModelsResponse = {
  fetched_at: number | null;
  tasks: string[];
  routing: Record<string, Record<string, string[]>>;
  models: ModelInfo[];
};

export type TranscriptSegment = {
  id: string;
  start_ms: number;
  end_ms: number;
  text: string;
  language: string | null;
  confidence: number | null;
  has_words: boolean;
  provenance: string;
};
export type TranscriptResponse = {
  transcript: { id: string; provider: string; model: string; language: string | null; precision: "word" | "segment"; edited: boolean; is_test: boolean };
  segments: TranscriptSegment[];
};

export type YouTubeInfo = {
  video_id: string;
  url: string;
  title: string;
  channel: string | null;
  duration_s: number | null;
  thumbnail_url: string | null;
  is_live: boolean;
};
export type LogLine = { id: number; stage: string | null; level: "info" | "warning" | "error"; message: string; ts: string };
export type Storage = { data_dir: string; bytes: Record<string, number>; total_bytes: number; cleanable: string[] };
