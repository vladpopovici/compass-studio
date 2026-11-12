// Minimal client for the draft API. The token comes from the URL fragment
// (#token=...), which the browser never sends to the server.

export interface LevelInfo { width: number; height: number; downsample: number }
export interface ImageInfo {
  id: string; width: number; height: number; tile_size: number;
  levels: LevelInfo[]; channels: string[]; mpp: number | null; kind: string;
}

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
  const r = await fetch(`api/v1/spike/results/${name}`, {
    method: "POST", headers: { ...authHeaders, "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  if (!r.ok) throw new Error(`results: ${r.status}`);
  return ((await r.json()) as { saved: string }).saved;
}
