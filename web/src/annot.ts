// bench=annot (S3): 5M points and 1M polygons resident, from the synthetic
// Arrow files served under /api/v1/spike/files.
//
// URL: /?bench=annot[&set=full][&layers=points,cells,regions][&tess=server|client]
//                   [&polymode=groups|filter]#token=...
//   tess=server  triangles precomputed (the generator's earcut, standing in
//                for server-side triangulation, D6); deck skips tessellation
//   tess=client  deck tessellates on the main thread
//   polymode=groups  one SolidPolygonLayer per group; group toggle = sub-layer
//                    visibility; one colour per group (default)
//   polymode=filter  one SolidPolygonLayer per layer with DataFilterExtension
//                    categories. Renders nothing in deck 9.4.1 (S3 finding,
//                    2026-11-13), kept to reproduce that.
//
// Points always use DataFilterExtension categories (works). With binary
// attributes deck uses category values as bit indices directly, so each
// layer's group ids are rebased to 0..k-1 and the first filterCategories
// lists them in order (deck maps categories to bits in order of first
// appearance). Limit: 128 groups per layer.
//
// Measures: fetch + parse, buffer build, time to first frame (layer init,
// upload, tessellation), GPU memory (luma.gl stats), per-frame render time
// with a GPU sync (gl.finish) during a scripted flight, group and layer
// toggle latency (setProps -> next rendered and synced frame), and pick
// latency and correctness.

import { Deck, OrthographicView, type Layer, type OrthographicViewState } from "@deck.gl/core";
import { DataFilterExtension } from "@deck.gl/extensions";
import { ScatterplotLayer, SolidPolygonLayer } from "@deck.gl/layers";
import { tableFromIPC, type Table } from "apache-arrow";
import { authHeaders, postResults } from "./api";
import { environment, nextFrame, percentiles } from "./env";

const params = new URLSearchParams(location.search);
const SET = params.get("set") ?? "full";
const LAYERS = (params.get("layers") ?? "points,cells,regions").split(",");
const TESS = params.get("tess") ?? "server";
const POLYMODE = params.get("polymode") ?? "groups";

const PALETTE: [number, number, number][] = [
  [230, 25, 75], [60, 180, 75], [255, 225, 25], [0, 130, 200], [245, 130, 48],
  [145, 30, 180], [70, 240, 240], [240, 50, 230], [210, 245, 60], [250, 190, 212],
];

interface Meta { params: { width: number; height: number };
  layers: { layer_id: number; name: string; groups: Record<string, string> }[] }

/** One deck layer. A logical layer (`parent`) is one or more of these. */
interface Part {
  id: string; parent: string; kind: "points" | "polygons";
  groupKey: number | null;          // polymode=groups: the group this part holds
  groups: number[];                 // filtered parts: rebased group keys 0..k-1
  count: number; vertices: number;
  objectIds: Uint32Array;
  positions: Float32Array;          // xy interleaved
  startIndices?: Uint32Array;       // polygons, n + 1 (vertex offsets)
  indices?: Uint32Array;            // polygons, global triangle indices
  colors?: Uint8Array;              // RGBA per point / per vertex (filtered parts)
  categories?: Uint32Array;         // group key per point / per vertex (filtered parts)
}

async function fetchTable(name: string): Promise<{ table: Table; bytes: number; ms: number }> {
  const t0 = performance.now();
  const r = await fetch(`api/v1/spike/files/${name}`, { headers: authHeaders });
  if (!r.ok) throw new Error(`${name}: ${r.status}`);
  const buf = new Uint8Array(await r.arrayBuffer());
  const table = tableFromIPC(buf);
  return { table, bytes: buf.byteLength, ms: performance.now() - t0 };
}

function layerMeta(meta: Meta, layerId: number) {
  const l = meta.layers.find((x) => x.layer_id === layerId)!;
  const ids = Object.keys(l.groups).map(Number).sort((a, b) => a - b);
  return { name: l.name, base: ids[0], k: ids.length };
}

function buildPoints(t: Table, meta: Meta, layerId: number): Part {
  const { name, base, k } = layerMeta(meta, layerId);
  const x = t.getChild("x")!.toArray() as Float32Array;
  const y = t.getChild("y")!.toArray() as Float32Array;
  const g = t.getChild("group")!.toArray() as Uint16Array;
  const layer = t.getChild("layer")!.toArray() as Uint8Array;
  const oid = t.getChild("object_id")!.toArray() as Uint32Array;
  let n = 0;
  for (let i = 0; i < layer.length; i++) if (layer[i] === layerId) n++;
  const positions = new Float32Array(2 * n), colors = new Uint8Array(4 * n);
  const categories = new Uint32Array(n), objectIds = new Uint32Array(n);
  for (let i = 0, j = 0; i < layer.length; i++) {
    if (layer[i] !== layerId) continue;
    const key = g[i] - base, c = PALETTE[key % PALETTE.length];
    positions[2 * j] = x[i]; positions[2 * j + 1] = y[i];
    colors[4 * j] = c[0]; colors[4 * j + 1] = c[1]; colors[4 * j + 2] = c[2]; colors[4 * j + 3] = 200;
    categories[j] = key; objectIds[j] = oid[i];
    j++;
  }
  return { id: name, parent: name, kind: "points", groupKey: null, groups: [...Array(k).keys()],
           count: n, vertices: n, objectIds, positions, colors, categories };
}

interface PolyRows {
  name: string; base: number; k: number; r0: number; r1: number;
  cOff: Int32Array; cVal: Float32Array; tOff: Int32Array; tVal: Uint32Array;
  g: Uint16Array; oid: Uint32Array;
}

function polyRows(t: Table, meta: Meta, layerId: number): PolyRows {
  const { name, base, k } = layerMeta(meta, layerId);
  const layer = t.getChild("layer")!.toArray() as Uint8Array;
  let r0 = -1, r1 = -1; // rows of one layer are contiguous
  for (let i = 0; i < layer.length; i++) if (layer[i] === layerId) { if (r0 < 0) r0 = i; r1 = i + 1; }
  const coords = t.getChild("coords")!.data[0], tris = t.getChild("triangles")!.data[0];
  return { name, base, k, r0, r1,
    cOff: coords.valueOffsets as Int32Array, cVal: coords.children[0].values as Float32Array,
    tOff: tris.valueOffsets as Int32Array, tVal: tris.children[0].values as Uint32Array,
    g: t.getChild("group")!.toArray() as Uint16Array,
    oid: t.getChild("object_id")!.toArray() as Uint32Array };
}

/** Rows [rows] of a polygon layer copied into one part. */
function polygonPart(p: PolyRows, rows: Int32Array, withIndices: boolean): Omit<Part, "id" | "parent" | "groupKey" | "groups"> {
  const n = rows.length;
  let nv = 0, nt = 0;
  for (const r of rows) { nv += (p.cOff[r + 1] - p.cOff[r]) / 2; nt += p.tOff[r + 1] - p.tOff[r]; }
  const positions = new Float32Array(2 * nv), startIndices = new Uint32Array(n + 1);
  const objectIds = new Uint32Array(n);
  const indices = withIndices ? new Uint32Array(nt) : undefined;
  let v = 0, ti = 0;
  for (let i = 0; i < n; i++) {
    const r = rows[i];
    startIndices[i] = v;
    positions.set(p.cVal.subarray(p.cOff[r], p.cOff[r + 1]), 2 * v);
    if (indices) for (let j = p.tOff[r]; j < p.tOff[r + 1]; j++) indices[ti++] = p.tVal[j] + v;
    v += (p.cOff[r + 1] - p.cOff[r]) / 2;
    objectIds[i] = p.oid[r];
  }
  startIndices[n] = v;
  return { kind: "polygons", count: n, vertices: nv, objectIds, positions, startIndices, indices };
}

function buildPolygons(t: Table, meta: Meta, layerId: number, withIndices: boolean): Part[] {
  const p = polyRows(t, meta, layerId);
  if (POLYMODE === "groups") {
    const byKey: number[][] = Array.from({ length: p.k }, () => []);
    for (let r = p.r0; r < p.r1; r++) byKey[p.g[r] - p.base].push(r);
    return byKey.map((rows, key) => ({
      id: `${p.name}/g${key}`, parent: p.name, groupKey: key, groups: [key],
      ...polygonPart(p, Int32Array.from(rows), withIndices),
    })).filter((x) => x.count > 0);
  }
  const rows = new Int32Array(p.r1 - p.r0).map((_, i) => p.r0 + i);
  const part: Part = { id: p.name, parent: p.name, groupKey: null, groups: [...Array(p.k).keys()],
                       ...polygonPart(p, rows, withIndices) };
  part.colors = new Uint8Array(4 * part.vertices);
  part.categories = new Uint32Array(part.vertices);
  for (let i = 0; i < part.count; i++) {
    const key = p.g[rows[i]] - p.base, c = PALETTE[key % PALETTE.length];
    for (let v = part.startIndices![i]; v < part.startIndices![i + 1]; v++) {
      part.colors.set([c[0], c[1], c[2], 110], 4 * v);
      part.categories[v] = key;
    }
  }
  return [part];
}

// Binary data and extension objects are created once per part: a new
// `data` object makes deck re-upload every attribute.
const binary = new WeakMap<Part, object>();
const EXT = [new DataFilterExtension({ categorySize: 1 })];

function binaryData(d: Part): object {
  let b = binary.get(d);
  if (!b) {
    const filtered = d.categories ? {
      getFillColor: { value: d.colors, size: 4, normalized: true },
      getFilterCategory: { value: d.categories, size: 1 } } : {};
    b = d.kind === "points"
      ? { length: d.count, attributes: { getPosition: { value: d.positions, size: 2 }, ...filtered } }
      : { length: d.count, startIndices: d.startIndices, attributes: {
          getPolygon: { value: d.positions, size: 2 },
          ...(d.indices ? { indices: d.indices } : {}), ...filtered } };
    binary.set(d, b);
  }
  return b;
}

function makeLayer(d: Part, visible: boolean, filter: number[]): Layer {
  const filtered = d.categories ? { extensions: EXT, filterCategories: filter } : {};
  if (d.kind === "points") {
    return new ScatterplotLayer({
      id: d.id, visible, pickable: true, ...filtered, data: binaryData(d),
      radiusUnits: "pixels", getRadius: 2, radiusMinPixels: 1, stroked: false,
    } as never);
  }
  const c = PALETTE[(d.groupKey ?? 0) % PALETTE.length];
  return new SolidPolygonLayer({
    id: d.id, visible, pickable: true, ...filtered,
    _normalize: false, positionFormat: "XY", data: binaryData(d),
    ...(d.categories ? {} : { getFillColor: [c[0], c[1], c[2], 110] }),
  } as never);
}

function pointInRing(px: number, py: number, pos: Float32Array, s: number, e: number): boolean {
  let inside = false;
  for (let i = s, j = e - 1; i < e; j = i++) {
    const xi = pos[2 * i], yi = pos[2 * i + 1], xj = pos[2 * j], yj = pos[2 * j + 1];
    if ((yi > py) !== (yj > py) && px < ((xj - xi) * (py - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

export async function runAnnot(hud: HTMLElement) {
  const say = (m: string) => { hud.textContent = `bench=annot ${SET} tess=${TESS} polymode=${POLYMODE}\n${m}`; };
  const timings: Record<string, number> = {};
  const mark = (k: string, t0: number) => { timings[k] = +(performance.now() - t0).toFixed(1); };

  say("fetching");
  const wantPoints = LAYERS.includes("points");
  const wantPolys = LAYERS.some((l) => l !== "points");
  const [pts, polys] = await Promise.all([
    wantPoints ? fetchTable(`${SET}.points.arrow`) : null,
    wantPolys ? fetchTable(`${SET}.polygons.arrow`) : null,
  ]);
  if (pts) timings.points_fetch_parse_ms = +pts.ms.toFixed(1);
  if (polys) timings.polygons_fetch_parse_ms = +polys.ms.toFixed(1);
  const meta = JSON.parse((pts ?? polys)!.table.schema.metadata.get("compass_studio.synth")!) as Meta;

  say("building buffers");
  let t0 = performance.now();
  const parts: Part[] = [];
  for (const l of meta.layers) {
    if (!LAYERS.includes(l.name)) continue;
    if (l.name === "points" && pts) parts.push(buildPoints(pts.table, meta, l.layer_id));
    else if (polys) parts.push(...buildPolygons(polys.table, meta, l.layer_id, TESS === "server"));
  }
  mark("build_buffers_ms", t0);
  const parents = [...new Set(parts.map((p) => p.parent))];

  const { width: W, height: H } = meta.params;
  const fit = Math.log2(Math.min(innerWidth / W, innerHeight / H));
  const vs = (x: number, y: number, z: number): OrthographicViewState => ({ target: [x, y, 0], zoom: z });
  let gl: WebGL2RenderingContext | null = null;
  let device: unknown = null;
  let renderStart = 0, sync = false;
  const renderMs: number[] = [];
  let rendered: (() => void) | null = null;
  const deck = new Deck({
    views: new OrthographicView({ id: "ortho", flipY: true }),
    viewState: vs(W / 2, H / 2, fit), controller: false,
    deviceProps: { type: "webgl", webgl: { preserveDrawingBuffer: false } },
    onBeforeRender: () => { renderStart = performance.now(); },
    onAfterRender: ({ gl: g, device: dev }) => {
      gl = g; device = dev;
      if (sync) g.finish();
      renderMs.push(performance.now() - renderStart);
      const r = rendered; rendered = null; r?.();
    },
  });
  // Resolves after the next frame deck renders following `change` (deck applies
  // setProps on its next animation frame), GPU-synced when `sync` is set.
  let stalls = 0; // waits that timed out: deck saw nothing to redraw
  const renderedAfter = (change: () => void) =>
    new Promise<void>((r) => {
      const timer = setTimeout(() => { stalls++; rendered = null; r(); }, 2000);
      rendered = () => { clearTimeout(timer); r(); };
      change();
    });
  const visible = Object.fromEntries(parts.map((d) => [d.id, true]));
  const filters = Object.fromEntries(parts.map((d) => [d.id, d.groups]));
  const setLayers = () => deck.setProps({ layers: parts.map((d) => makeLayer(d, visible[d.id], filters[d.id])) });

  say("first frame (init, upload, tessellation)");
  await nextFrame();
  t0 = performance.now();
  sync = true;
  await renderedAfter(setLayers);
  mark("first_frame_ms", t0);

  const gpuMemory = () => {
    const stats = (device as { statsManager?: { getStats(n: string): { getTable(): Record<string, { count?: number }> } } })
      ?.statsManager?.getStats("GPU Time and Memory").getTable() ?? {};
    return Object.fromEntries(Object.entries(stats).filter(([k]) => k.endsWith("Memory"))
      .map(([k, v]) => [k, +((v.count ?? 0) / 2 ** 20).toFixed(1)]));
  };
  const memAfterInit = gpuMemory();

  // flight: frame intervals and synced render time per phase
  const cx = W / 2, cy = H / 2;
  const phases: [string, number, [number, number, number], [number, number, number]][] = [
    ["pan-fit", 3, [cx - W / 8, cy, fit], [cx + W / 8, cy, fit]],
    ["zoom-in", 3, [cx, cy, fit], [cx, cy, -2]],
    ["pan-z-2", 3, [cx, cy, -2], [cx + 1500 * 3 * 4, cy, -2]],
    ["pan-z-4", 3, [cx, cy, -4], [cx, cy + 1500 * 3 * 16, -4]],
    ["pan-z0", 3, [cx, cy, 0], [cx + 1500 * 3, cy, 0]],
    ["zoom-out", 3, [cx, cy, 0], [cx, cy, fit]],
  ];
  const flight: Record<string, unknown>[] = [];
  for (const [name, sec, a, b] of phases) {
    say(`flight ${name}`);
    const dts: number[] = [];
    renderMs.length = 0;
    const s0 = await nextFrame();
    let last = s0;
    for (;;) {
      const now = await nextFrame();
      const u = Math.min(1, (now - s0) / (sec * 1000));
      deck.setProps({ viewState: vs(a[0] + (b[0] - a[0]) * u, a[1] + (b[1] - a[1]) * u, a[2] + (b[2] - a[2]) * u) });
      if (now > s0) dts.push(now - last);
      last = now;
      if (u >= 1) break;
    }
    flight.push({ phase: name, frames: dts.length, frame_ms: percentiles(dts),
                  render_sync_ms: percentiles([...renderMs]) });
  }

  // toggles at the fit view (everything on screen)
  await renderedAfter(() => deck.setProps({ viewState: vs(cx + 1, cy, fit) }));
  const toggles: Record<string, number[]> = {};
  for (const parent of parents) {
    say(`toggles ${parent}`);
    const own = parts.filter((p) => p.parent === parent);
    const groupMs: number[] = [], layerMs: number[] = [];
    const timed = async () => { const s = performance.now(); await renderedAfter(setLayers); return performance.now() - s; };
    for (const p of own) {
      for (const g of p.groups) {
        for (const on of [false, true]) {
          if (p.groupKey === null) filters[p.id] = on ? p.groups : p.groups.filter((x) => x !== g);
          else visible[p.id] = on;
          groupMs.push(await timed());
        }
      }
    }
    for (let i = 0; i < 4; i++) {
      for (const p of own) visible[p.id] = !visible[p.id];
      layerMs.push(await timed());
    }
    toggles[`${parent}_group_toggle_ms`] = groupMs;
    toggles[`${parent}_layer_toggle_ms`] = layerMs;
  }
  sync = false;

  // picking: centre the view on a random object (polygons: the mean of its
  // vertices), pick at the screen centre, and check the picked object
  // contains (polygon) or is within 3.5 px of (point) that location.
  // Overlaps mean the picked object may differ from the chosen one.
  const picks: Record<string, unknown>[] = [];
  let rng = 12345;
  const rand = () => ((rng = (rng * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff);
  const total = parts.reduce((a, d) => a + d.count, 0);
  for (const z of [-2, 0, 2]) {
    say(`picking zoom ${z}`);
    const ms: number[] = [];
    let hits = 0, correct = 0, sameObject = 0;
    const misses: Record<string, number> = {};
    for (let i = 0; i < 100; i++) {
      let r = Math.floor(rand() * total), li = 0;
      while (r >= parts[li].count) { r -= parts[li].count; li++; }
      const d0 = parts[li];
      let ox = 0, oy = 0;
      if (d0.kind === "points") { ox = d0.positions[2 * r]; oy = d0.positions[2 * r + 1]; }
      else {
        const s0 = d0.startIndices![r], e0 = d0.startIndices![r + 1];
        for (let k = s0; k < e0; k++) { ox += d0.positions[2 * k]; oy += d0.positions[2 * k + 1]; }
        ox /= e0 - s0; oy /= e0 - s0;
      }
      await renderedAfter(() => deck.setProps({ viewState: vs(ox, oy, z) }));
      const s = performance.now();
      const info = deck.pickObject({ x: Math.floor(innerWidth / 2), y: Math.floor(innerHeight / 2), radius: 0 });
      ms.push(performance.now() - s);
      if (!info || !info.layer) { misses[d0.parent] = (misses[d0.parent] ?? 0) + 1; continue; }
      hits++;
      const d = parts.find((x) => x.id === info.layer!.id)!;
      const k = info.index;
      if (d.objectIds[k] === d0.objectIds[r]) sameObject++;
      if (d.kind === "points") {
        const dx = (d.positions[2 * k] - ox) * 2 ** z, dy = (d.positions[2 * k + 1] - oy) * 2 ** z;
        if (Math.hypot(dx, dy) <= 3.5) correct++;
      } else if (pointInRing(ox, oy, d.positions, d.startIndices![k], d.startIndices![k + 1])) correct++;
    }
    picks.push({ zoom: z, pick_ms: percentiles(ms), hits, correct, same_object: sameObject, misses });
  }

  const counts: Record<string, { objects: number; vertices: number; triangles: number | null; parts: number }> = {};
  for (const p of parts) {
    const c = (counts[p.parent] ??= { objects: 0, vertices: 0, triangles: p.indices ? 0 : null, parts: 0 });
    c.objects += p.count; c.vertices += p.vertices; c.parts++;
    if (p.indices) c.triangles = (c.triangles ?? 0) + p.indices.length / 3;
  }
  const result = {
    spike: "S3", bench: "annot", set: SET, layers: LAYERS, tess: TESS, polymode: POLYMODE, counts,
    arrow_bytes: { points: pts?.bytes ?? 0, polygons: polys?.bytes ?? 0 },
    timings, gpu_memory_mb_after_init: memAfterInit, gpu_memory_mb_end: gpuMemory(),
    flight,
    toggles: Object.fromEntries(Object.entries(toggles).map(([k, v]) => [k, percentiles(v)])),
    picks, render_wait_timeouts: stalls, environment: environment(gl),
    deck_version: (await import("@deck.gl/core")).VERSION,
  };
  const saved = await postResults("s3-annot", result);
  say(`done -> ${saved}\n${JSON.stringify({ timings, memAfterInit }, null, 1)}`);
  (window as unknown as { __benchDone?: unknown }).__benchDone = result;
  deck.finalize();
}
