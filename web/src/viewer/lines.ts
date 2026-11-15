// Polyline layers (milestone 6): the whole layer's polylines as one Arrow
// buffer (`line-layer` v1: object_id, group, coords), fetched once, drawn as
// binary PathLayers, one per group (toggled by visibility and coloured by a
// constant, so neither needs a buffer rewrite). Width in µm per band.

import type { Layer } from "@deck.gl/core";
import { PathLayer } from "@deck.gl/layers";
import { tableFromIPC, type Table } from "apache-arrow";
import { authHeaders, type LayerInfo } from "../api";
import { groupColor, rgb, type AnnSource, type DrawContext, type ObjectPick } from "./annstyle";

/** Arrow list column of a one-batch table: value offsets (absolute indices into `values`) and values. */
export function listColumn<T extends Float32Array | Uint32Array>(table: Table, name: string): { offsets: Int32Array; values: T } {
  const c = table.getChild(name);
  if (!c) throw new Error(`no column ${name}`);
  if (c.data.length !== 1) throw new Error(`${name}: expected one record batch`);
  const d = c.data[0];
  const offsets = (d.valueOffsets as Int32Array).subarray(d.offset, d.offset + d.length + 1);
  return { offsets, values: d.children[0].values as T };
}

/** Paths of one group: vertices, path starts (in vertices) and object ids. */
export interface GroupPaths {
  group: number;
  positions: Float32Array;
  /** start of each path, length `count` + 1 */
  starts: Uint32Array;
  objectIds: Float64Array;
  count: number;
  /** deck binary data, made once: a new object makes deck re-upload the buffers */
  data?: object;
}

/** Binary PathLayer data for a set of paths (cached on it). */
export function pathData(p: GroupPaths): object {
  return (p.data ??= { length: p.count, startIndices: p.starts.subarray(0, p.count),
                       attributes: { getPath: { value: p.positions, size: 2 } } });
}

export function pathVertices(p: GroupPaths, i: number): Float32Array {
  return p.positions.slice(2 * p.starts[i], 2 * p.starts[i + 1]);
}

export function bboxCentre(v: Float32Array): [number, number] {
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (let i = 0; i < v.length; i += 2) {
    x0 = Math.min(x0, v[i]); x1 = Math.max(x1, v[i]);
    y0 = Math.min(y0, v[i + 1]); y1 = Math.max(y1, v[i + 1]);
  }
  return [(x0 + x1) / 2, (y0 + y1) / 2];
}

export class LineSource implements AnnSource {
  loaded = false;
  count = 0;
  private byGroup: GroupPaths[] = [];
  private counts: number[] = [];

  constructor(readonly info: LayerInfo) {}

  get layerId() {
    return this.info.id;
  }

  private deckId(group: number) {
    return `lines-${this.info.id}-${group}`;
  }

  async load(signal?: AbortSignal) {
    const r = await fetch(`api/v1/layers/${encodeURIComponent(this.info.id)}/lines`, { headers: authHeaders, signal });
    if (!r.ok) throw new Error(`lines ${this.info.id}: ${r.status}`);
    const table = tableFromIPC(new Uint8Array(await r.arrayBuffer()));
    const n = table.numRows;
    const ids = table.getChild("object_id")!.toArray() as BigInt64Array;
    const groups = table.getChild("group")!.toArray() as Uint16Array;
    const { offsets, values } = listColumn<Float32Array>(table, "coords");
    const k = this.info.groups.length;
    const nPaths = new Array(k).fill(0), nVerts = new Array(k).fill(0);
    for (let i = 0; i < n; i++) {
      nPaths[groups[i]]++;
      nVerts[groups[i]] += (offsets[i + 1] - offsets[i]) / 2;
    }
    const out: GroupPaths[] = Array.from({ length: k }, (_, g) => ({
      group: g, positions: new Float32Array(2 * nVerts[g]), starts: new Uint32Array(nPaths[g] + 1),
      objectIds: new Float64Array(nPaths[g]), count: 0,
    }));
    for (let i = 0; i < n; i++) {
      const p = out[groups[i]], v = p.starts[p.count];
      p.positions.set(values.subarray(offsets[i], offsets[i + 1]), 2 * v);
      p.objectIds[p.count] = Number(ids[i]);
      p.count++;
      p.starts[p.count] = v + (offsets[i + 1] - offsets[i]) / 2;
    }
    this.byGroup = out.filter((p) => p.count > 0);
    this.counts = nPaths;
    this.count = n;
    this.loaded = true;
  }

  groupCounts(): number[] {
    return this.counts;
  }

  layers({ style, mpp, band }: DrawContext): Layer[] {
    if (!this.loaded || !style.visible) return [];
    const hidden = new Set(style.hidden);
    return this.byGroup.filter((p) => !hidden.has(p.group)).map((p) => new PathLayer({
      id: this.deckId(p.group),
      data: pathData(p) as never,
      _pathType: "open",
      positionFormat: "XY",
      getColor: [...rgb(groupColor(style, p.group)), 255],
      opacity: style.opacity,
      widthUnits: "common",
      getWidth: style.widths[band] / mpp,
      widthMinPixels: 1,
      capRounded: true,
      jointRounded: true,
      pickable: true,
      autoHighlight: true,
      highlightColor: [255, 255, 255, 160],
    }));
  }

  pick(deckLayerId: string, index: number): ObjectPick | null {
    const p = this.byGroup.find((x) => this.deckId(x.group) === deckLayerId);
    if (!p || index < 0 || index >= p.count) return null;
    const path = pathVertices(p, index);
    const [x, y] = bboxCentre(path);
    return { layerId: this.info.id, objectId: p.objectIds[index], group: p.group, kind: "polyline", x, y,
             shape: { paths: [path], closed: false } };
  }
}
