// Polygon layers with level of detail (milestone 6, decision D6).
//
// Zoomed in (shown level <= the layer's polygon_max_level, 3): polygon
// tiles (`polygon-tile` v1) are always fetched on the level-3 grid (4096 x
// 4096 level-0 px per tile), whatever the zoom: few requests (S4: under
// 15 ms p95 server time per tile). A polygon comes in every tile its
// bounding box meets, so the tiles in view (plus a one-tile ring) are
// merged into one set of buffers per group with each polygon once,
// rebuilt when that set of tiles changes. Large regions are never lost
// and nothing is drawn twice.
//
// Zoomed out: centroid tiles (`centroid-tile` v1, each object in exactly
// one tile) at the shown level, drawn as points (PointBuffers) sized by the
// point bands.
//
// Polygons are drawn one SolidPolygonLayer (fill, server triangles) and one
// PathLayer (outline rings) per group: DataFilterExtension categories draw
// nothing on binary SolidPolygonLayer data (S3), and per-group layers make
// toggles and colour changes prop changes, with no buffer rewrite.

import type { Layer } from "@deck.gl/core";
import { PathLayer, SolidPolygonLayer } from "@deck.gl/layers";
import { tableFromIPC } from "apache-arrow";
import { authHeaders, type ImageInfo, type LayerInfo } from "../api";
import { groupColor, levelAt, rgb, type AnnSource, type DrawContext, type ObjectPick } from "./annstyle";
import { bboxCentre, listColumn, pathData, type GroupPaths } from "./lines";
import { PointBuffers } from "./points";
import type { View } from "./viewer";

const TILE = 512;
const MAX_INFLIGHT = 6;
const MAX_CACHED = 400;

interface ShapeTile {
  kind: "shapes";
  n: number;
  ids: BigInt64Array;
  groups: Uint16Array;
  coords: { offsets: Int32Array; values: Float32Array };
  holes: { offsets: Int32Array; values: Uint32Array };
  tris: { offsets: Int32Array; values: Uint32Array };
}

interface CentreTile {
  kind: "centres";
  n: number;
  ids: BigInt64Array;
  groups: Uint16Array;
  x: Float32Array;
  y: Float32Array;
}

type Tile = ShapeTile | CentreTile;

interface Entry {
  key: string;
  state: "loading" | "ready" | "error";
  tile?: Tile;
  used: number;
}

/** Polygons of one group, merged: rings drawn as outlines share the vertex buffer. */
interface GroupShapes {
  group: number;
  count: number;
  positions: Float32Array;
  /** vertex start of each polygon, length count + 1 */
  starts: Uint32Array;
  /** global triangle indices */
  indices: Uint32Array;
  objectIds: Float64Array;
  /** rings as paths (exterior and holes) */
  rings: GroupPaths;
  /** polygon of each ring */
  ringPolygon: Uint32Array;
  /** first ring of each polygon, length count + 1 */
  polygonRings: Uint32Array;
  fillData?: object;
}

function parse(buf: Uint8Array): Tile {
  const t = tableFromIPC(buf);
  const schema = t.schema.metadata.get("schema");
  const ids = t.getChild("object_id")!.toArray() as BigInt64Array;
  const groups = t.getChild("group")!.toArray() as Uint16Array;
  if (schema === "polygon-tile")
    return { kind: "shapes", n: t.numRows, ids, groups, coords: listColumn<Float32Array>(t, "coords"),
             holes: listColumn<Uint32Array>(t, "holes"), tris: listColumn<Uint32Array>(t, "triangles") };
  if (schema === "centroid-tile")
    return { kind: "centres", n: t.numRows, ids, groups, x: t.getChild("x")!.toArray() as Float32Array,
             y: t.getChild("y")!.toArray() as Float32Array };
  throw new Error(`unexpected tile schema ${schema}`);
}

export class PolygonSource implements AnnSource {
  onChange?: () => void;
  /** for measurements */
  stats = { tiles: 0, polygons: 0, rebuildMs: 0, rebuilds: 0, pending: 0 };
  private entries = new Map<string, Entry>();
  private needed: string[] = [];
  private neededKey = "";
  private mode: "shapes" | "centres" = "shapes";
  private queue: string[] = [];
  private inflight = 0;
  private ac = new AbortController();
  private clock = 0;
  private scheduled = false;
  private shapes: GroupShapes[] = [];
  private centres: PointBuffers | null = null;
  private drawn: "shapes" | "centres" | null = null;

  constructor(readonly info: LayerInfo) {}

  get layerId() {
    return this.info.id;
  }

  setView(view: View, image: ImageInfo) {
    const level = levelAt(view.zoom);
    const mode = level <= this.info.polygon_max_level ? "shapes" : "centres";
    const grid = mode === "shapes" ? this.info.polygon_max_level : Math.min(level, image.levels.length - 1);
    const side = TILE * 2 ** grid, s = 2 ** view.zoom;
    const cols = Math.ceil(image.width / side), rows = Math.ceil(image.height / side);
    const c0 = Math.max(0, Math.floor((view.x - view.width / 2 / s) / side) - 1);
    const c1 = Math.min(cols - 1, Math.floor((view.x + view.width / 2 / s) / side) + 1);
    const r0 = Math.max(0, Math.floor((view.y - view.height / 2 / s) / side) - 1);
    const r1 = Math.min(rows - 1, Math.floor((view.y + view.height / 2 / s) / side) + 1);
    const key = `${grid}/${c0}-${c1}/${r0}-${r1}`;
    if (key === this.neededKey) return;
    this.neededKey = key;
    this.mode = mode;
    const cc = (c0 + c1) / 2, rc = (r0 + r1) / 2, want: [string, number][] = [];
    for (let c = c0; c <= c1; c++)
      for (let r = r0; r <= r1; r++) want.push([`${grid}/${c}/${r}`, (c - cc) ** 2 + (r - rc) ** 2]);
    want.sort((a, b) => a[1] - b[1]);
    this.needed = want.map(([k]) => k);
    const t = ++this.clock;
    for (const k of this.needed) {
      const e = this.entries.get(k);
      if (e) e.used = t;
      else {
        this.entries.set(k, { key: k, state: "loading", used: t });
        this.queue.push(k);
      }
    }
    // queued tiles no longer in view are dropped (they fetch again if needed)
    const want2 = new Set(this.needed);
    this.queue = this.queue.filter((k) => {
      if (want2.has(k)) return true;
      this.entries.delete(k);
      return false;
    });
    this.queue.sort((a, b) => this.needed.indexOf(a) - this.needed.indexOf(b));
    this.evict(want2);
    this.pump();
    this.schedule();
  }

  private evict(keep: Set<string>) {
    if (this.entries.size <= MAX_CACHED) return;
    const old = [...this.entries.values()].filter((e) => e.state !== "loading" && !keep.has(e.key))
      .sort((a, b) => a.used - b.used);
    for (const e of old.slice(0, this.entries.size - MAX_CACHED)) this.entries.delete(e.key);
  }

  private pump() {
    while (this.inflight < MAX_INFLIGHT && this.queue.length) {
      const key = this.queue.shift()!;
      this.inflight++;
      fetch(`api/v1/layers/${encodeURIComponent(this.info.id)}/tiles/${key}`, { headers: authHeaders, signal: this.ac.signal })
        .then(async (r) => {
          const e = this.entries.get(key);
          if (!e) return;
          if (r.status === 404) e.tile = { kind: "centres", n: 0, ids: new BigInt64Array(0), groups: new Uint16Array(0),
                                           x: new Float32Array(0), y: new Float32Array(0) };
          else if (!r.ok) throw new Error(`${r.status}`);
          else e.tile = parse(new Uint8Array(await r.arrayBuffer()));
          e.state = "ready";
        })
        .catch(() => {
          const e = this.entries.get(key);
          if (e) e.state = "error";
        })
        .finally(() => {
          this.inflight--;
          this.pump();
          if (this.needed.includes(key)) this.schedule();
        });
    }
    this.stats.pending = this.queue.length + this.inflight;
  }

  /** Rebuild at most once per frame, however many tiles arrive. */
  private schedule() {
    if (this.scheduled) return;
    this.scheduled = true;
    requestAnimationFrame(() => {
      this.scheduled = false;
      this.rebuild();
    });
  }

  private rebuild() {
    const tiles = this.needed.map((k) => this.entries.get(k)?.tile)
      .filter((t): t is Tile => !!t && (t.kind === (this.mode === "shapes" ? "shapes" : "centres") || t.n === 0));
    // nothing of the new set yet: keep drawing the old one
    if (!tiles.length && this.drawn) return;
    const t0 = performance.now();
    if (this.mode === "shapes") {
      this.shapes = this.mergeShapes(tiles.filter((t): t is ShapeTile => t.kind === "shapes"));
      this.stats.polygons = this.shapes.reduce((a, g) => a + g.count, 0);
    } else {
      this.centres = this.mergeCentres(tiles.filter((t): t is CentreTile => t.kind === "centres"));
      this.stats.polygons = this.centres.count;
    }
    this.drawn = this.mode;
    this.stats.tiles = tiles.length;
    this.stats.rebuildMs = performance.now() - t0;
    this.stats.rebuilds++;
    this.stats.pending = this.queue.length + this.inflight;
    this.onChange?.();
  }

  private mergeCentres(tiles: CentreTile[]): PointBuffers {
    const n = tiles.reduce((a, t) => a + t.n, 0);
    const pos = new Float32Array(2 * n), groups = new Uint32Array(n), ids = new Float64Array(n);
    let i = 0;
    for (const t of tiles)
      for (let j = 0; j < t.n; j++, i++) {
        pos[2 * i] = t.x[j]; pos[2 * i + 1] = t.y[j];
        groups[i] = t.groups[j];
        ids[i] = Number(t.ids[j]);
      }
    return new PointBuffers(this.info, `centres-${this.info.id}`, "polygon", pos, groups, ids);
  }

  private mergeShapes(tiles: ShapeTile[]): GroupShapes[] {
    const k = this.info.groups.length;
    const seen = new Set<number>();
    const picks: [ShapeTile, number][][] = Array.from({ length: k }, () => []);
    const nPoly = new Array(k).fill(0), nVert = new Array(k).fill(0), nIdx = new Array(k).fill(0), nRing = new Array(k).fill(0);
    for (const t of tiles)
      for (let j = 0; j < t.n; j++) {
        const id = Number(t.ids[j]);
        if (seen.has(id)) continue;
        seen.add(id);
        const g = t.groups[j];
        picks[g].push([t, j]);
        nPoly[g]++;
        nVert[g] += (t.coords.offsets[j + 1] - t.coords.offsets[j]) / 2;
        nIdx[g] += t.tris.offsets[j + 1] - t.tris.offsets[j];
        nRing[g] += 1 + t.holes.offsets[j + 1] - t.holes.offsets[j];
      }
    const out: GroupShapes[] = [];
    for (let g = 0; g < k; g++) {
      if (!nPoly[g]) continue;
      const positions = new Float32Array(2 * nVert[g]);
      const starts = new Uint32Array(nPoly[g] + 1), indices = new Uint32Array(nIdx[g]);
      const objectIds = new Float64Array(nPoly[g]);
      const ringStarts = new Uint32Array(nRing[g] + 1), ringPolygon = new Uint32Array(nRing[g]);
      const polygonRings = new Uint32Array(nPoly[g] + 1);
      let v = 0, ix = 0, ring = 0;
      picks[g].forEach(([t, j], p) => {
        const c0 = t.coords.offsets[j], c1 = t.coords.offsets[j + 1], nv = (c1 - c0) / 2;
        positions.set(t.coords.values.subarray(c0, c1), 2 * v);
        starts[p] = v;
        objectIds[p] = Number(t.ids[j]);
        for (let q = t.tris.offsets[j]; q < t.tris.offsets[j + 1]; q++) indices[ix++] = v + t.tris.values[q];
        polygonRings[p] = ring;
        ringStarts[ring] = v;
        ringPolygon[ring++] = p;
        for (let q = t.holes.offsets[j]; q < t.holes.offsets[j + 1]; q++) {
          ringStarts[ring] = v + t.holes.values[q];
          ringPolygon[ring++] = p;
        }
        v += nv;
      });
      starts[nPoly[g]] = v;
      ringStarts[ring] = v;
      polygonRings[nPoly[g]] = ring;
      out.push({ group: g, count: nPoly[g], positions, starts, indices, objectIds, ringPolygon, polygonRings,
                 rings: { group: g, positions, starts: ringStarts, objectIds, count: ring } });
    }
    return out;
  }

  private fillId(g: number) {
    return `poly-${this.info.id}-${g}`;
  }

  private outlineId(g: number) {
    return `poly-${this.info.id}-${g}-outline`;
  }

  layers(ctx: DrawContext): Layer[] {
    const { style, mpp, band } = ctx;
    if (!style.visible) return [];
    if (this.drawn === "centres") return this.centres ? [this.centres.layer(ctx)] : [];
    const hidden = new Set(style.hidden), out: Layer[] = [];
    for (const s of this.shapes) {
      if (hidden.has(s.group)) continue;
      const c = rgb(groupColor(style, s.group));
      if (style.fillOpacity > 0)
        out.push(new SolidPolygonLayer({
          id: this.fillId(s.group),
          data: (s.fillData ??= { length: s.count, startIndices: s.starts.subarray(0, s.count),
                                  attributes: { getPolygon: { value: s.positions, size: 2 }, indices: s.indices } }) as never,
          _normalize: false,
          positionFormat: "XY",
          getFillColor: [...c, Math.round(255 * style.fillOpacity)],
          opacity: style.opacity,
          pickable: true,
          autoHighlight: true,
          highlightColor: [255, 255, 255, 90],
        }));
      if (style.outline)
        out.push(new PathLayer({
          id: this.outlineId(s.group),
          data: pathData(s.rings) as never,
          _pathType: "loop",
          positionFormat: "XY",
          getColor: [...c, 255],
          opacity: style.opacity,
          widthUnits: "common",
          getWidth: style.widths[band] / mpp,
          widthMinPixels: 1,
          pickable: true,
          autoHighlight: style.fillOpacity === 0,
          highlightColor: [255, 255, 255, 160],
        }));
    }
    return out;
  }

  pick(deckLayerId: string, index: number): ObjectPick | null {
    if (this.centres && deckLayerId === this.centres.deckId) return this.centres.pick(index);
    for (const s of this.shapes) {
      let p = -1;
      if (deckLayerId === this.fillId(s.group)) p = index;
      else if (deckLayerId === this.outlineId(s.group)) p = s.ringPolygon[index] ?? -1;
      if (p < 0 || p >= s.count) continue;
      const paths: Float32Array[] = [];
      for (let r = s.polygonRings[p]; r < s.polygonRings[p + 1]; r++)
        paths.push(s.positions.slice(2 * s.rings.starts[r], 2 * s.rings.starts[r + 1]));
      const [x, y] = bboxCentre(paths[0]);
      return { layerId: this.info.id, objectId: s.objectIds[p], group: s.group, kind: "polygon", x, y,
               shape: { paths, closed: true } };
    }
    return null;
  }

  destroy() {
    this.ac.abort();
  }
}
