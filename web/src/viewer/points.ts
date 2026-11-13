// Point layers (milestone 5, decision D6): the whole layer's points as one
// Arrow buffer (`point-layer` v1: object_id, x, y, group), drawn as a binary
// ScatterplotLayer, filtered by group on the GPU and picked by index.
//
// Group filtering uses DataFilterExtension categories (S3). With binary
// attributes deck compares the raw attribute value with bits of a mask it
// builds from `filterCategories`, assigning bits to categories in order of
// first appearance; `GroupFilter` makes that mapping the identity, so the
// bit is the group index whatever order groups are shown or hidden in.
// Limit: 128 groups per layer (deck's category mask); above that the layer
// draws every group.
//
// Colours live in their own attribute buffer; a colour change rewrites and
// re-uploads only that buffer (a new descriptor object for `getFillColor`,
// plus `colorVersion` in updateTriggers).

import { DataFilterExtension } from "@deck.gl/extensions";
import { ScatterplotLayer } from "@deck.gl/layers";
import { tableFromIPC } from "apache-arrow";
import { authHeaders, type LayerInfo } from "../api";

export interface PointStyle {
  visible: boolean;
  /** group indices not drawn */
  hidden: number[];
  /** "#rrggbb" per group index; missing entries use the default palette */
  colors: Record<number, string>;
  /** radius in screen px */
  size: number;
  opacity: number;
}

export const DEFAULT_POINT_STYLE: PointStyle = { visible: true, hidden: [], colors: {}, size: 3, opacity: 0.9 };

// Okabe-Ito, then Tableau 10 (colour-blind friendly first)
export const GROUP_PALETTE = [
  "#e69f00", "#56b4e9", "#009e73", "#f0e442", "#0072b2", "#d55e00", "#cc79a7",
  "#4e79a7", "#f28e2b", "#e15759", "#76b7b2", "#59a14f", "#edc948", "#b07aa1", "#ff9da7", "#9c755f",
];

export const MAX_FILTER_GROUPS = 128;
/** points keep their full size down to zoom 1 / SHRINK_BELOW, then shrink with the slide */
const SHRINK_BELOW = 4;

export function groupColor(style: PointStyle, index: number): string {
  return style.colors[index] ?? GROUP_PALETTE[index % GROUP_PALETTE.length];
}

function rgb(hex: string): [number, number, number] {
  const n = parseInt(hex.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

class GroupFilter extends DataFilterExtension {
  static extensionName = "GroupFilter";
  _getCategoryKey(category: number): number {
    return category;
  }
}
const EXTENSIONS = [new GroupFilter({ categorySize: 1 })];

export interface PointPick {
  layerId: string;
  index: number;
  objectId: number;
  group: number;
  x: number;
  y: number;
}

export class PointSource {
  count = 0;
  positions = new Float32Array(0);
  groups = new Uint32Array(0);
  objectIds: BigInt64Array | Float64Array = new Float64Array(0);
  revision = 0;
  loaded = false;
  timing: { fetchMs: number; buildMs: number; bytes: number } | null = null;
  private colors = new Uint8Array(0);
  private colorKey = "";
  private colorVersion = 0;
  private data: object | null = null;

  constructor(readonly info: LayerInfo) {}

  get id() {
    return this.info.id;
  }

  /** Fetch and unpack the layer's points; returns the time taken in ms. */
  async load(signal?: AbortSignal): Promise<{ fetchMs: number; buildMs: number; bytes: number }> {
    const t0 = performance.now();
    const r = await fetch(`api/v1/layers/${encodeURIComponent(this.info.id)}/points`, { headers: authHeaders, signal });
    if (!r.ok) throw new Error(`points ${this.info.id}: ${r.status}`);
    const buf = new Uint8Array(await r.arrayBuffer());
    const t1 = performance.now();
    const table = tableFromIPC(buf);
    const n = table.numRows;
    const col = (name: string) => {
      const c = table.getChild(name);
      if (!c) throw new Error(`points ${this.info.id}: no column ${name}`);
      return c.toArray();
    };
    const xs = col("x") as Float32Array, ys = col("y") as Float32Array, gs = col("group") as Uint16Array;
    const positions = new Float32Array(2 * n);
    for (let i = 0; i < n; i++) {
      positions[2 * i] = xs[i];
      positions[2 * i + 1] = ys[i];
    }
    this.positions = positions;
    this.groups = Uint32Array.from(gs);
    this.objectIds = col("object_id") as BigInt64Array;
    this.count = n;
    this.revision = Number(table.schema.metadata.get("revision") ?? 0);
    this.colors = new Uint8Array(4 * n);
    this.colorKey = "";
    this.data = null;
    this.loaded = true;
    this.timing = { fetchMs: t1 - t0, buildMs: performance.now() - t1, bytes: buf.byteLength };
    return this.timing;
  }

  objectId(index: number): number {
    return Number(this.objectIds[index]);
  }

  pick(index: number): PointPick {
    return { layerId: this.id, index, objectId: this.objectId(index), group: this.groups[index],
             x: this.positions[2 * index], y: this.positions[2 * index + 1] };
  }

  /** Index of an object id (linear scan; for restoring selections). */
  indexOf(objectId: number): number {
    const ids = this.objectIds;
    for (let i = 0; i < this.count; i++) if (Number(ids[i]) === objectId) return i;
    return -1;
  }

  /** Points per group index. */
  groupCounts(): number[] {
    const out = new Array(this.info.groups.length).fill(0);
    for (let i = 0; i < this.count; i++) out[this.groups[i]]++;
    return out;
  }

  private updateColors(style: PointStyle) {
    const k = this.info.groups.length;
    const key = JSON.stringify(Array.from({ length: k }, (_, i) => groupColor(style, i)));
    if (key === this.colorKey) return;
    this.colorKey = key;
    const table = Array.from({ length: k }, (_, i) => rgb(groupColor(style, i)));
    const c = this.colors, g = this.groups;
    for (let i = 0; i < this.count; i++) {
      const [r, gg, b] = table[g[i]] ?? [128, 128, 128];
      c[4 * i] = r; c[4 * i + 1] = gg; c[4 * i + 2] = b; c[4 * i + 3] = 255;
    }
    // same `data` object, new descriptor for this attribute: deck keeps the
    // other buffers and re-uploads this one (it compares descriptors by identity)
    this.colors = new Uint8Array(c);
    if (this.data) (this.data as { attributes: Record<string, object> }).attributes.getFillColor =
      { value: this.colors, size: 4, normalized: true };
    this.colorVersion++;
  }

  private binary(): object {
    // one `data` object per load: a new one makes deck re-upload every attribute
    this.data ??= {
      length: this.count,
      attributes: {
        getPosition: { value: this.positions, size: 2 },
        getFillColor: { value: this.colors, size: 4, normalized: true },
        getFilterCategory: { value: this.groups, size: 1 },
      },
    };
    return this.data;
  }

  layer(style: PointStyle) {
    this.updateColors(style);
    const filter = this.info.groups.length <= MAX_FILTER_GROUPS;
    const hidden = new Set(style.hidden);
    const shown = this.info.groups.map((g) => g.index).filter((i) => !hidden.has(i));
    return new ScatterplotLayer({
      id: `points-${this.id}`,
      data: this.binary() as never,
      visible: style.visible && this.loaded,
      opacity: style.opacity,
      pickable: true,
      autoHighlight: true,
      highlightColor: [255, 255, 255, 220],
      // `size` screen px from 1:4 zoom inward; zoomed out further, points
      // shrink with the slide (to 1 px), which keeps millions of
      // overlapping points from saturating the GPU at fit (fill rate)
      radiusUnits: "common",
      getRadius: style.size * SHRINK_BELOW,
      radiusMinPixels: 1,
      radiusMaxPixels: style.size,
      stroked: false,
      updateTriggers: { getFillColor: this.colorVersion },
      ...(filter ? { extensions: EXTENSIONS, filterCategories: shown } : {}),
    });
  }
}

/** Ring around selected points (screen px, drawn above the point layers):
 * a dark ring under a white one, visible on light and dark points alike. */
export function selectionLayers(picks: PointPick[], radius: number) {
  const r = Math.max(radius + 3, 6);
  return [["dark", [0, 0, 0, 255], 4], ["light", [255, 255, 255, 255], 2]].map(([name, color, width]) =>
    new ScatterplotLayer<PointPick>({
      id: `points-selection-${name}`,
      data: picks,
      getPosition: (p) => [p.x, p.y],
      radiusUnits: "pixels",
      getRadius: r,
      stroked: true,
      filled: false,
      lineWidthUnits: "pixels",
      getLineWidth: width as number,
      getLineColor: color as [number, number, number, number],
      updateTriggers: { getRadius: r },
    }));
}
