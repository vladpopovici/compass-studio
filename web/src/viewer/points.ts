// Point layers (milestone 5, decision D6): the whole layer's points as one
// Arrow buffer (`point-layer` v1: object_id, x, y, group), drawn as a binary
// ScatterplotLayer, filtered by group on the GPU and picked by index.
// `PointBuffers` also draws polygon centres when zoomed out (polygons.ts).
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
//
// Objects changed by edits (milestone 7) are hidden here and drawn by the
// edit overlay: a 0/1 filter value per point (DataFilterExtension value
// filter, range [-0.5, 0.5]), present only while something is hidden.

import { DataFilterExtension } from "@deck.gl/extensions";
import { ScatterplotLayer } from "@deck.gl/layers";
import { tableFromIPC } from "apache-arrow";
import { authHeaders, type LayerInfo } from "../api";
import { groupColor, rgb, type AnnSource, type AnnStyle, type DrawContext, type ObjectKind, type ObjectPick } from "./annstyle";

export const MAX_FILTER_GROUPS = 128;

class GroupFilter extends DataFilterExtension {
  static extensionName = "GroupFilter";
  _getCategoryKey(category: number): number {
    return category;
  }
}
const EXTENSIONS = [new GroupFilter({ categorySize: 1 })];
const EXTENSIONS_HIDE = [new GroupFilter({ categorySize: 1, filterSize: 1 })];
const HIDE_ONLY = [new DataFilterExtension({ filterSize: 1 })];

/** Positions, groups and ids of points (or polygon centres), drawn as one ScatterplotLayer. */
export class PointBuffers {
  readonly count: number;
  private colors: Uint8Array;
  private colorKey = "";
  private colorVersion = 0;
  private data: { length: number; attributes: Record<string, object> };
  private hiding = false;
  private hideVersion = -1;
  private hideCount = 0;
  /** point indices sorted by object id, made on first hide */
  private order: Uint32Array | null = null;

  constructor(readonly info: LayerInfo, readonly deckId: string, readonly kind: ObjectKind,
              readonly positions: Float32Array, readonly groups: Uint32Array,
              readonly objectIds: ArrayLike<number | bigint>) {
    this.count = groups.length;
    this.colors = new Uint8Array(4 * this.count);
    // one `data` object per buffer set: a new one makes deck re-upload every attribute
    this.data = {
      length: this.count,
      attributes: {
        getPosition: { value: positions, size: 2 },
        getFillColor: { value: this.colors, size: 4, normalized: true },
        getFilterCategory: { value: groups, size: 1 },
      },
    };
  }

  objectId(index: number): number {
    return Number(this.objectIds[index]);
  }

  /** Index of an object id, or -1. */
  indexOf(id: number): number {
    if (!this.order) {
      const ids = Float64Array.from(this.objectIds as ArrayLike<number>, Number);
      this.order = Uint32Array.from({ length: this.count }, (_, i) => i).sort((a, b) => ids[a] - ids[b]);
    }
    let lo = 0, hi = this.count - 1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1, v = this.objectId(this.order[mid]);
      if (v === id) return this.order[mid];
      if (v < id) lo = mid + 1; else hi = mid - 1;
    }
    return -1;
  }

  /** Hide these objects (edited ones, drawn by the overlay); `version` changes with the set. */
  setHidden(ids: ReadonlySet<number>, version: number) {
    if (version === this.hideVersion) return;
    this.hideVersion = version;
    if (!ids.size && !this.hiding) return;
    const v = new Float32Array(this.count);
    let n = 0;
    for (const id of ids) {
      const i = this.indexOf(id);
      if (i >= 0) { v[i] = 1; n++; }
    }
    this.hiding = n > 0;
    this.hideCount++;
    if (this.hiding) this.data.attributes.getFilterValue = { value: v, size: 1 };
    else delete this.data.attributes.getFilterValue;
  }

  /** Is this deck layer id these buffers' layer? */
  owns(deckLayerId: string): boolean {
    return deckLayerId === this.deckId;
  }

  pick(index: number): ObjectPick {
    return { layerId: this.info.id, objectId: this.objectId(index), group: this.groups[index], kind: this.kind,
             x: this.positions[2 * index], y: this.positions[2 * index + 1] };
  }

  /** Points per group index. */
  groupCounts(): number[] {
    const out = new Array(this.info.groups.length).fill(0);
    for (let i = 0; i < this.count; i++) out[this.groups[i]]++;
    return out;
  }

  private updateColors(style: AnnStyle) {
    const k = this.info.groups.length;
    const key = JSON.stringify(Array.from({ length: k }, (_, i) => groupColor(style, i)));
    if (key === this.colorKey) return;
    this.colorKey = key;
    const table = Array.from({ length: k }, (_, i) => rgb(groupColor(style, i)));
    const c = new Uint8Array(4 * this.count), g = this.groups;
    for (let i = 0; i < this.count; i++) {
      const [r, gg, b] = table[g[i]] ?? [128, 128, 128];
      c[4 * i] = r; c[4 * i + 1] = gg; c[4 * i + 2] = b; c[4 * i + 3] = 255;
    }
    // same `data` object, new descriptor for this attribute: deck keeps the
    // other buffers and re-uploads this one (it compares descriptors by identity)
    this.colors = c;
    this.data.attributes.getFillColor = { value: c, size: 4, normalized: true };
    this.colorVersion++;
  }

  layer({ style, mpp, band }: DrawContext) {
    this.updateColors(style);
    const filter = this.info.groups.length <= MAX_FILTER_GROUPS;
    const hidden = new Set(style.hidden);
    const shown = this.info.groups.map((g) => g.index).filter((i) => !hidden.has(i));
    const ext = this.hiding ? (filter ? EXTENSIONS_HIDE : HIDE_ONLY) : filter ? EXTENSIONS : null;
    return new ScatterplotLayer({
      id: this.deckId,
      data: this.data as never,
      visible: style.visible,
      opacity: style.opacity,
      pickable: true,
      autoHighlight: true,
      highlightColor: [255, 255, 255, 220],
      // common units are level-0 px: true size in µm; at least 1 px across
      radiusUnits: "common",
      getRadius: style.sizes[band] / 2 / mpp,
      radiusMinPixels: 0.5,
      stroked: false,
      updateTriggers: { getFillColor: this.colorVersion, getFilterValue: this.hideCount },
      ...(ext ? { extensions: ext } : {}),
      ...(filter ? { filterCategories: shown } : {}),
      ...(this.hiding ? { filterRange: [-0.5, 0.5] } : {}),
    });
  }
}

/** A whole point layer, fetched once (`GET /layers/{id}/points`). */
export class PointSource implements AnnSource {
  buffers: PointBuffers | null = null;
  revision = 0;
  private hidden: [ReadonlySet<number>, number] = [new Set(), 0];
  timing: { fetchMs: number; buildMs: number; bytes: number } | null = null;

  constructor(readonly info: LayerInfo) {}

  get layerId() {
    return this.info.id;
  }

  get count() {
    return this.buffers?.count ?? 0;
  }

  get loaded() {
    return this.buffers !== null;
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
    this.buffers = new PointBuffers(this.info, `points-${this.info.id}`, "point", positions, Uint32Array.from(gs),
                                    col("object_id") as BigInt64Array);
    this.buffers.setHidden(...this.hidden);
    this.revision = Number(table.schema.metadata.get("revision") ?? 0);
    this.timing = { fetchMs: t1 - t0, buildMs: performance.now() - t1, bytes: buf.byteLength };
    return this.timing;
  }

  groupCounts(): number[] {
    return this.buffers?.groupCounts() ?? [];
  }

  setHidden(ids: ReadonlySet<number>, version: number) {
    this.hidden = [ids, version];
    this.buffers?.setHidden(ids, version);
  }

  layers(ctx: DrawContext) {
    return this.buffers ? [this.buffers.layer(ctx)] : [];
  }

  pick(deckLayerId: string, index: number): ObjectPick | null {
    return this.buffers?.owns(deckLayerId) ? this.buffers.pick(index) : null;
  }
}
