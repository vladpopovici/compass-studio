// Image tile layer over the tile server (contract v1.1: cacheable tiles via
// ?v=version), with tunable loading and per-tile timing.

import { TileLayer, _Tileset2D as Tileset2D } from "@deck.gl/geo-layers";
import { BitmapLayer } from "@deck.gl/layers";
import { authHeaders, type ImageInfo } from "../api";
import { percentiles } from "./stats";

/** How tiles are requested and shown; see `TILE_PRESETS`. */
export interface TileOptions {
  /** concurrent requests issued by deck's scheduler (the browser adds its own 6 per host) */
  maxRequests: number;
  /** ms without new tile requests before queued ones are sent (deck `debounceTime`) */
  debounceTime: number;
  /** extra ring of tiles around the viewport at the same level, loaded at lower priority */
  prefetchRing: number;
  /** most tiles handed to the GPU per frame (texture uploads); Infinity = no limit */
  uploadsPerFrame: number;
  /** tiles kept by deck's cache; null = deck's default (5x the visible set) */
  maxCacheSize: number | null;
}

export const TILE_PRESETS: Record<string, TileOptions> = {
  // what the S2 spike measured
  s2: { maxRequests: 12, debounceTime: 0, prefetchRing: 0, uploadsPerFrame: Infinity, maxCacheSize: null },
  // milestone 3 default (see docs/decisions.md D5)
  tuned: { maxRequests: 6, debounceTime: 0, prefetchRing: 1, uploadsPerFrame: Infinity, maxCacheSize: null },
  // single-change variants, measured against the two above (bench.html?bench=pan&tiles=...)
  "req6": { maxRequests: 6, debounceTime: 0, prefetchRing: 0, uploadsPerFrame: Infinity, maxCacheSize: null },
  "gate2": { maxRequests: 12, debounceTime: 0, prefetchRing: 0, uploadsPerFrame: 2, maxCacheSize: null },
  "ring1": { maxRequests: 12, debounceTime: 0, prefetchRing: 1, uploadsPerFrame: Infinity, maxCacheSize: null },
  "debounce": { maxRequests: 12, debounceTime: 60, prefetchRing: 0, uploadsPerFrame: Infinity, maxCacheSize: null },
};

interface TileRec { t0: number; loaded?: number; drawn?: number; read?: number; encode?: number; failed?: boolean }

export class TileStats {
  recs: TileRec[] = [];
  inflight = 0;
  cached = 0;

  async fetchTile(path: string, signal?: AbortSignal): Promise<ImageBitmap> {
    return this.fetchAs(path, signal, async (r) => createImageBitmap(await r.blob()));
  }

  /** The tile's bytes, timed like an image tile (label maps). */
  async fetchBytes(path: string, signal?: AbortSignal): Promise<Uint8Array> {
    return this.fetchAs(path, signal, async (r) => new Uint8Array(await r.arrayBuffer()));
  }

  private async fetchAs<T>(path: string, signal: AbortSignal | undefined, decode: (r: Response) => Promise<T>): Promise<T> {
    const rec: TileRec = { t0: performance.now() };
    this.recs.push(rec);
    this.inflight++;
    try {
      const r = await fetch(`api/v1/${path}`, { headers: authHeaders, signal });
      if (!r.ok) throw new Error(`${r.status}`);
      const timing = r.headers.get("server-timing");
      if (!timing) this.cached++; // served from the browser cache
      for (const part of (timing ?? "").split(",")) {
        const m = part.trim().match(/^(read|encode);dur=([\d.]+)/);
        if (m) rec[m[1] as "read" | "encode"] = parseFloat(m[2]);
      }
      const out = await decode(r);
      rec.loaded = performance.now();
      return out;
    } catch (e) {
      rec.failed = true;
      throw e;
    } finally {
      this.inflight--;
    }
  }

  markDrawn(now: number) {
    for (const r of this.recs) if (r.loaded !== undefined && r.drawn === undefined) r.drawn = now;
  }

  summary() {
    const ok = this.recs.filter((r) => r.loaded !== undefined);
    return {
      requested: this.recs.length, loaded: ok.length, from_browser_cache: this.cached,
      failed_or_aborted: this.recs.filter((r) => r.failed).length,
      request_to_bitmap_ms: percentiles(ok.map((r) => r.loaded! - r.t0)),
      request_to_screen_ms: percentiles(ok.filter((r) => r.drawn).map((r) => r.drawn! - r.t0)),
      server_read_ms: percentiles(ok.filter((r) => r.read !== undefined).map((r) => r.read!)),
      server_encode_ms: percentiles(ok.filter((r) => r.encode !== undefined).map((r) => r.encode!)),
    };
  }

  summaryLine() {
    const s = this.summary();
    return `tiles ${s.loaded}/${s.requested} inflight ${this.inflight}  ` +
      `p95 to screen ${s.request_to_screen_ms.p95 ?? "-"} ms`;
  }
}

/** Releases at most `perFrame` waiters per animation frame. */
class FrameGate {
  private queue: (() => void)[] = [];
  private scheduled = false;
  constructor(private perFrame: number) {}

  wait(): Promise<void> {
    if (!Number.isFinite(this.perFrame)) return Promise.resolve();
    return new Promise((resolve) => {
      this.queue.push(resolve);
      this.schedule();
    });
  }

  private schedule() {
    if (this.scheduled) return;
    this.scheduled = true;
    requestAnimationFrame(() => {
      this.scheduled = false;
      for (let i = 0; i < this.perFrame && this.queue.length; i++) this.queue.shift()!();
      if (this.queue.length) this.schedule();
    });
  }
}

/** Tileset that also selects a ring of neighbouring tiles at the same level. */
export function ringTileset(ring: number, cols: (z: number) => number, rows: (z: number) => number) {
  return class RingTileset extends Tileset2D {
    getTileIndices(opts: Parameters<Tileset2D["getTileIndices"]>[0]) {
      const base = super.getTileIndices(opts);
      if (!ring || !base.length) return base;
      const z = base[0].z;
      let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
      for (const t of base) {
        if (t.z !== z) return base;
        x0 = Math.min(x0, t.x); y0 = Math.min(y0, t.y); x1 = Math.max(x1, t.x); y1 = Math.max(y1, t.y);
      }
      const out = [...base];
      const nx = cols(z), ny = rows(z);
      for (let y = Math.max(0, y0 - ring); y <= Math.min(ny - 1, y1 + ring); y++)
        for (let x = Math.max(0, x0 - ring); x <= Math.min(nx - 1, x1 + ring); x++)
          if (x < x0 || x > x1 || y < y0 || y > y1) out.push({ x, y, z });
      return out;
    }
  };
}

export function fitZoom(info: ImageInfo, width = innerWidth, height = innerHeight): number {
  return Math.log2(Math.min(width / info.width, height / info.height));
}

export function makeTileLayer(info: ImageInfo, stats: TileStats, bust = "",
                              options: TileOptions = TILE_PRESETS.tuned, idSuffix = "") {
  const n = info.levels.length;
  const ts = info.tile_size;
  const gate = new FrameGate(options.uploadsPerFrame);
  const level = (z: number) => info.levels[Math.min(n - 1, Math.max(0, -z))];
  const query = new URLSearchParams();
  if (info.version) query.set("v", info.version);
  if (bust) query.set("r", bust);
  const qs = query.size ? `?${query}` : "";
  return new TileLayer<ImageBitmap>({
    id: `tiles-${info.id}-${bust}${idSuffix}`,
    tileSize: ts,
    extent: [0, 0, info.width, info.height],
    minZoom: -(n - 1),
    maxZoom: 0,
    refinementStrategy: "best-available",
    // never let loaders.gl fetch worker scripts from its default CDN (D10)
    loadOptions: { worker: false },
    maxRequests: options.maxRequests,
    debounceTime: options.debounceTime,
    maxCacheSize: options.maxCacheSize,
    TilesetClass: ringTileset(options.prefetchRing,
                              (z) => Math.ceil(level(z).width / ts), (z) => Math.ceil(level(z).height / ts)),
    getTileData: async ({ index: { x, y, z }, signal }) => {
      const bmp = await stats.fetchTile(`images/${info.id}/tiles/${-z}/${x}/${y}.jpg${qs}`, signal);
      await gate.wait();
      return bmp;
    },
    onTileUnload: (tile) => (tile.content as ImageBitmap | null)?.close?.(),
    renderSubLayers: (props) => {
      const image = props.data as ImageBitmap | null;
      if (!image) return null;
      const { x, y, z } = props.tile.index;
      const s = 2 ** -z;
      const x0 = x * ts * s, y0 = y * ts * s;
      return new BitmapLayer({
        id: `${props.id}-bmp`,
        image,
        bounds: [x0, y0 + image.height * s, x0 + image.width * s, y0],
        textureParameters: { minFilter: "linear", magFilter: "linear" },
        loadOptions: { worker: false },
      });
    },
  });
}
