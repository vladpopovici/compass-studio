// Tile layer over the tile server, with per-tile timing.

import { TileLayer } from "@deck.gl/geo-layers";
import { BitmapLayer } from "@deck.gl/layers";
import { authHeaders, type ImageInfo } from "./api";
import { percentiles } from "./env";

interface TileRec { t0: number; loaded?: number; drawn?: number; read?: number; encode?: number; failed?: boolean }

export class TileStats {
  recs: TileRec[] = [];
  inflight = 0;

  async fetchTile(path: string, signal?: AbortSignal): Promise<ImageBitmap> {
    const rec: TileRec = { t0: performance.now() };
    this.recs.push(rec);
    this.inflight++;
    try {
      const r = await fetch(`api/v1/${path}`, { headers: authHeaders, signal });
      if (!r.ok) throw new Error(`${r.status}`);
      for (const part of (r.headers.get("server-timing") ?? "").split(",")) {
        const m = part.trim().match(/^(read|encode);dur=([\d.]+)/);
        if (m) rec[m[1] as "read" | "encode"] = parseFloat(m[2]);
      }
      const bmp = await createImageBitmap(await r.blob());
      rec.loaded = performance.now();
      return bmp;
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
      requested: this.recs.length, loaded: ok.length,
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

export function fitZoom(info: ImageInfo): number {
  return Math.log2(Math.min(innerWidth / info.width, innerHeight / info.height));
}

export function makeTileLayer(info: ImageInfo, stats: TileStats, bust = "") {
  const n = info.levels.length;
  return new TileLayer<ImageBitmap>({
    id: `tiles-${info.id}-${bust}`,
    tileSize: info.tile_size,
    extent: [0, 0, info.width, info.height],
    minZoom: -(n - 1),
    maxZoom: 0,
    refinementStrategy: "best-available",
    maxRequests: 12,
    getTileData: ({ index: { x, y, z }, signal }) =>
      stats.fetchTile(`images/${info.id}/tiles/${-z}/${x}/${y}.jpg${bust ? `?r=${bust}` : ""}`,
                      signal),
    renderSubLayers: (props) => {
      const image = props.data as ImageBitmap | null;
      if (!image) return null;
      const { x, y, z } = props.tile.index;
      const s = 2 ** -z;
      const x0 = x * info.tile_size * s, y0 = y * info.tile_size * s;
      return new BitmapLayer({
        id: `${props.id}-bmp`,
        image,
        bounds: [x0, y0 + image.height * s, x0 + image.width * s, y0],
        textureParameters: { minFilter: "linear", magFilter: "linear" },
      });
    },
  });
}
