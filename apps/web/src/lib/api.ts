import type { ApiErrorBody } from "./types";

// The browser talks to the local API directly (large uploads, ranged video playback).
// No credential ever lives in the frontend: the OpenRouter key is read server-side only.
export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";

export class ApiError extends Error {
  code: string;
  status: number;
  details: Record<string, unknown>;

  constructor(status: number, body: Partial<ApiErrorBody>) {
    super(body.message ?? `Erreur ${status}`);
    this.status = status;
    this.code = body.code ?? "unknown";
    this.details = body.details ?? {};
  }
}

const UNREACHABLE = "API injoignable — vérifiez que le serveur CLIPFACTORY est démarré (scripts/dev.sh).";

async function parse<T>(response: Response): Promise<T> {
  if (response.ok) {
    return (await response.json()) as T;
  }
  let body: Partial<ApiErrorBody> = {};
  try {
    const json = await response.json();
    body = json.error ?? { message: typeof json.detail === "string" ? json.detail : "Requête invalide" };
  } catch {
    // non-JSON error body: keep the status only
  }
  throw new ApiError(response.status, body);
}

export async function api<T>(path: string, init?: { method?: string; body?: unknown }): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_URL}/api${path}`, {
      method: init?.method ?? "GET",
      headers: init?.body !== undefined ? { "Content-Type": "application/json" } : undefined,
      body: init?.body !== undefined ? JSON.stringify(init.body) : undefined,
    });
  } catch {
    throw new ApiError(0, { code: "unreachable", message: UNREACHABLE });
  }
  return parse<T>(response);
}

/** Absolute URL for a media or download path returned by the API. */
export const mediaUrl = (path: string | null | undefined): string | undefined => (path ? `${API_URL}${path}` : undefined);

/** Streams a file to the API with real upload progress (fetch cannot report it). */
export function uploadSource<T>(file: File, onProgress: (fraction: number) => void): { promise: Promise<T>; abort: () => void } {
  const xhr = new XMLHttpRequest();
  const promise = new Promise<T>((resolve, reject) => {
    xhr.open("POST", `${API_URL}/api/sources/upload?filename=${encodeURIComponent(file.name)}`);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress(event.loaded / event.total);
    };
    xhr.onload = () => {
      let json: { error?: Partial<ApiErrorBody> } & Record<string, unknown> = {};
      try {
        json = JSON.parse(xhr.responseText);
      } catch {
        // fall through with an empty body
      }
      if (xhr.status >= 200 && xhr.status < 300) resolve(json as T);
      else reject(new ApiError(xhr.status, json.error ?? {}));
    };
    xhr.onerror = () => reject(new ApiError(0, { code: "unreachable", message: UNREACHABLE }));
    xhr.onabort = () => reject(new ApiError(0, { code: "aborted", message: "Envoi annulé" }));
    xhr.send(file);
  });
  return { promise, abort: () => xhr.abort() };
}
