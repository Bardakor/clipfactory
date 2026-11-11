"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { api } from "./api";
import type { Clip, ClipDetail, Health, Job, LogLine, ModelsResponse, Project, RuntimeSettings, Stats, Storage, TranscriptResponse } from "./types";

const ACTIVE = new Set(["queued", "running"]);

export const useHealth = () =>
  useQuery({ queryKey: ["health"], queryFn: () => api<Health>("/health"), refetchInterval: 10_000, retry: false });

export const useStats = () => useQuery({ queryKey: ["stats"], queryFn: () => api<Stats>("/stats"), refetchInterval: 15_000 });

export const useProjects = () =>
  useQuery({
    queryKey: ["projects"],
    queryFn: async () => (await api<{ projects: Project[] }>("/projects")).projects,
    refetchInterval: (query) => (query.state.data?.some((p) => ACTIVE.has(p.status)) ? 3000 : 20_000),
  });

/** Polls quickly while anything is running for the project, slowly otherwise. */
export const useProject = (id: string) =>
  useQuery({
    queryKey: ["project", id],
    queryFn: async () => (await api<{ project: Project }>(`/projects/${id}`)).project,
    refetchInterval: (query) => {
      const project = query.state.data;
      const busy = project && (ACTIVE.has(project.status) || (project.render_jobs?.length ?? 0) > 0);
      return busy ? 1200 : 15_000;
    },
  });

export const useJobLogs = (jobId: string | null | undefined, live: boolean) =>
  useQuery({
    queryKey: ["logs", jobId],
    queryFn: async () => (await api<{ logs: LogLine[] }>(`/jobs/${jobId}/logs`)).logs,
    enabled: Boolean(jobId),
    refetchInterval: live ? 1500 : false,
  });

export const useClips = (params: { projectId?: string; approval?: string; sort: string }) =>
  useQuery({
    queryKey: ["clips", params],
    queryFn: async () => {
      const search = new URLSearchParams({ sort: params.sort });
      if (params.projectId) search.set("project_id", params.projectId);
      if (params.approval) search.set("approval", params.approval);
      return (await api<{ clips: Clip[] }>(`/clips?${search}`)).clips;
    },
    refetchInterval: 10_000,
  });

export const useClip = (id: string) =>
  useQuery({
    queryKey: ["clip", id],
    queryFn: () => api<ClipDetail>(`/clips/${id}`),
    refetchInterval: (query) => (query.state.data?.active_job ? 1000 : false),
  });

export const useTranscript = (sourceId: string | undefined, startMs: number, endMs: number) =>
  useQuery({
    queryKey: ["transcript", sourceId, startMs, endMs],
    queryFn: () => api<TranscriptResponse>(`/sources/${sourceId}/transcript?start_ms=${startMs}&end_ms=${endMs}`),
    enabled: Boolean(sourceId),
    retry: false,
  });

export const useRuntimeSettings = () =>
  useQuery({ queryKey: ["settings"], queryFn: async () => (await api<{ settings: RuntimeSettings }>("/settings")).settings });

export const useModels = () => useQuery({ queryKey: ["models"], queryFn: () => api<ModelsResponse>("/models"), staleTime: 300_000 });

export const useStorage = () => useQuery({ queryKey: ["storage"], queryFn: () => api<Storage>("/storage") });

/** A mutation that refreshes everything it could have affected and reports failures as toasts. */
export function useAction<TInput, TOutput = unknown>(run: (input: TInput) => Promise<TOutput>, options?: { success?: string; onDone?: (output: TOutput) => void }) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: run,
    onSuccess: (output) => {
      if (options?.success) toast.success(options.success);
      void client.invalidateQueries();
      options?.onDone?.(output);
    },
    onError: (error: Error) => toast.error(error.message),
  });
}

export const jobActions = {
  cancel: (jobId: string) => api<{ job: Job }>(`/jobs/${jobId}/cancel`, { method: "POST" }),
  retry: (jobId: string) => api<{ job: Job }>(`/jobs/${jobId}/retry`, { method: "POST" }),
};

export const clipActions = {
  feedback: (clipId: string, body: { decision: string; rejection_reason?: string; rating?: number; social_metrics?: Record<string, number> }) =>
    api<{ clip: Clip }>(`/clips/${clipId}/feedback`, { method: "POST", body }),
  render: (clipId: string, mode: "preview" | "final") => api<{ job: Job }>(`/clips/${clipId}/render`, { method: "POST", body: { mode } }),
  patch: (clipId: string, body: Record<string, unknown>) => api<{ clip: ClipDetail["clip"] }>(`/clips/${clipId}`, { method: "PATCH", body }),
};
