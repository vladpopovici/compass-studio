// Minimal client for contract v1 (types generated from contract/openapi.yaml
// by `pnpm gen:api`). The token comes from the URL fragment (#token=...),
// which the browser never sends to the server.

import type { components } from "./contract/v1";

export type Schemas = components["schemas"];
export type ImageInfo = Schemas["Image"];
export type LayerInfo = Schemas["Layer"];

const token = new URLSearchParams(location.hash.slice(1)).get("token") ?? "";
export const authHeaders = { Authorization: `Bearer ${token}` };

export async function getJSON<T>(path: string): Promise<T> {
  const r = await fetch(`api/v1/${path}`, { headers: authHeaders });
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return r.json() as Promise<T>;
}

export async function getBlob(path: string, signal?: AbortSignal): Promise<Blob> {
  const r = await fetch(`api/v1/${path}`, { headers: authHeaders, signal });
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return r.blob();
}

export async function postResults(name: string, data: unknown): Promise<string> {
  const r = await fetch(`api/spike/results/${name}`, {
    method: "POST", headers: { ...authHeaders, "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  if (!r.ok) throw new Error(`results: ${r.status}`);
  return ((await r.json()) as { saved: string }).saved;
}

/** POST JSON; resolves with the status and parsed body (also for 4xx), rejects on network errors. */
export async function postJSON<T = unknown>(path: string, body: unknown): Promise<{ ok: boolean; status: number; data: T }> {
  const r = await fetch(`api/v1/${path}`, {
    method: "POST", headers: { ...authHeaders, "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  let data: T;
  try {
    data = (await r.json()) as T;
  } catch {
    data = {} as T;
  }
  return { ok: r.ok, status: r.status, data };
}
