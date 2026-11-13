// bench=edit (S5): editable-layers on an orthographic canvas, with the 1M-cell
// bulk layer (and 200 large regions) resident behind it.
//
// URL: /?bench=edit[&set=full][&store=synth]#token=...
//      server: --spike-files (the Arrow bulk) and --annotations synth=<the same
//      store>; the cells and regions layers are forked through the v1 API on
//      first use (POST /layers/{id}/fork), keeping their object ids.
//
// Flow per object (D8): pick it in the bulk layer -> check out (GET
// /layers/{edit layer}/features) -> hide it in the bulk layer by a GPU value filter (only the
// object's vertex range of the filter buffer is rewritten) -> edit it in an
// EditableGeoJsonLayer (ModifyMode) -> drag one vertex with synthetic pointer
// events -> commit (POST /stores/{store}/commit, update_geometry) -> show the committed
// geometry in an overlay layer. Then draw a new polygon (DrawPolygonMode) and
// commit it as a create.
//
// Measured: check-out round trip (request -> edit layer rendered), commit round
// trip (request -> overlay rendered), frame interval and render time while
// dragging a vertex of small cells and of the largest regions, draw + create.

import { COORDINATE_SYSTEM, Deck, OrthographicView, type Layer, type OrthographicViewState } from "@deck.gl/core";
import { DataFilterExtension } from "@deck.gl/extensions";
import { GeoJsonLayer, SolidPolygonLayer } from "@deck.gl/layers";
import { DrawPolygonMode, EditableGeoJsonLayer, ModifyMode } from "@deck.gl-community/editable-layers";
import { buildPolygons, fetchTable, PALETTE, type Meta, type Part } from "./annot";
import { authHeaders, postResults, type LayerInfo } from "./api";
import { environment, nextFrame, percentiles } from "./env";

const params = new URLSearchParams(location.search);
const SET = params.get("set") ?? "full";
const STORE = params.get("store") ?? "synth";

type FC = { type: "FeatureCollection"; features: Feature[]; revision?: number };
type Feature = { type: "Feature"; id?: number; geometry: { type: string; coordinates: number[][][] };
                 properties: Record<string, unknown> };

const EXT = [new DataFilterExtension({ filterSize: 1 })];

async function api<T>(path: string, init?: RequestInit): Promise<{ status: number; body: T }> {
  const r = await fetch(`api/v1/${path}`, { ...init, headers: { ...authHeaders, "Content-Type": "application/json" } });
  return { status: r.status, body: (await r.json()) as T };
}

export async function runEdit(hud: HTMLElement) {
  const say = (m: string) => { hud.textContent = `bench=edit ${SET}\n${m}`; };
  const log: string[] = [];

  say("loading bulk polygons");
  const polys = await fetchTable(`${SET}.polygons.arrow`);
  const meta = JSON.parse(polys.table.schema.metadata.get("compass_studio.synth")!) as Meta;
  const parts: Part[] = [];
  for (const l of meta.layers) if (l.name !== "points") parts.push(...buildPolygons(polys.table, meta, l.layer_id, true));
  // object id -> (part, index)
  const where = new Map<number, [Part, number]>();
  for (const p of parts) for (let i = 0; i < p.count; i++) where.set(p.objectIds[i], [p, i]);

  const { width: W, height: H } = meta.params;
  let gl: WebGL2RenderingContext | null = null;
  let renderStart = 0;
  const renderMs: number[] = [];
  let rendered: (() => void) | null = null;
  let viewState: OrthographicViewState = { target: [W / 2, H / 2, 0], zoom: -6 };
  const deck = new Deck({
    views: new OrthographicView({ id: "ortho", flipY: true }),
    viewState, controller: { dragPan: true, scrollZoom: false, doubleClickZoom: false },
    onViewStateChange: ({ viewState: v }) => { viewState = v as OrthographicViewState; deck.setProps({ viewState }); },
    onBeforeRender: () => { renderStart = performance.now(); },
    onAfterRender: ({ gl: g }) => {
      gl = g; g.finish();
      renderMs.push(performance.now() - renderStart);
      const r = rendered; rendered = null; r?.();
    },
  });
  const renderedAfter = (change: () => void) => new Promise<void>((r) => {
    const t = setTimeout(() => { rendered = null; r(); }, 2000);
    rendered = () => { clearTimeout(t); r(); };
    change();
  });
  while (!(deck as unknown as { device?: unknown }).device) await nextFrame();
  const device = (deck as unknown as { device: { createBuffer(p: object): { write(d: ArrayBufferView, off?: number): void } } }).device;

  // per-part filter buffers: 0 = visible, 1 = checked out (hidden)
  const filterBuf = new Map<Part, { write(d: ArrayBufferView, off?: number): void }>();
  const binary = new Map<Part, object>();
  for (const p of parts) {
    const buf = device.createBuffer({ data: new Float32Array(p.vertices) });
    filterBuf.set(p, buf);
    binary.set(p, { length: p.count, startIndices: p.startIndices, attributes: {
      getPolygon: { value: p.positions, size: 2 }, indices: p.indices,
      getFilterValue: { buffer: buf, size: 1, type: "float32" } } });
  }
  const bulkLayers = () => parts.map((p) => {
    const c = PALETTE[(p.groupKey ?? 0) % PALETTE.length];
    return new SolidPolygonLayer({ id: p.id, data: binary.get(p), _normalize: false, positionFormat: "XY",
      pickable: true, extensions: EXT, filterRange: [-0.5, 0.5], getFillColor: [c[0], c[1], c[2], 110] } as never);
  });
  const setHidden = (oid: number, hidden: boolean) => {
    const [p, i] = where.get(oid)!;
    const s = p.startIndices![i], e = p.startIndices![i + 1];
    filterBuf.get(p)!.write(new Float32Array(e - s).fill(hidden ? 1 : 0), s * 4);
  };

  let editData: FC = { type: "FeatureCollection", features: [] };
  let editMode: unknown = ModifyMode;
  let selected: number[] = [];
  const committed: Feature[] = [];
  let lastEdit = "";
  const editLayer = () => new EditableGeoJsonLayer({
    id: "edit", data: editData, mode: editMode, selectedFeatureIndexes: selected,
    coordinateSystem: COORDINATE_SYSTEM.CARTESIAN,
    getFillColor: [255, 255, 255, 60], getLineColor: [255, 255, 255, 255], lineWidthMinPixels: 2,
    editHandlePointRadiusMinPixels: 4,
    onEdit: ({ updatedData, editType }: { updatedData: FC; editType: string }) => {
      lastEdit = editType;
      editData = updatedData;
      setLayers();
    },
  } as never);
  const overlay = () => new GeoJsonLayer({ id: "committed", data: { type: "FeatureCollection", features: committed },
    getFillColor: [255, 200, 0, 140], getLineColor: [255, 200, 0, 255], lineWidthMinPixels: 1 } as never);
  const setLayers = () => deck.setProps({ layers: [...bulkLayers(), overlay(), editLayer()] as Layer[] });

  say("first frame");
  let t0 = performance.now();
  await renderedAfter(setLayers);
  const firstFrame = performance.now() - t0;

  const canvas = deck.getCanvas()!;
  const project = (x: number, y: number) => {
    const vp = deck.getViewports()[0];
    const [sx, sy] = vp.project([x, y]);
    const r = canvas.getBoundingClientRect();
    return [r.left + sx, r.top + sy];
  };
  const fire = (type: string, x: number, y: number, buttons: number) => {
    const ev = new PointerEvent(type, { bubbles: true, cancelable: true, clientX: x, clientY: y,
      pointerId: 1, pointerType: "mouse", isPrimary: true, button: 0, buttons });
    canvas.dispatchEvent(ev);
  };
  const setView = (x: number, y: number, z: number) =>
    renderedAfter(() => { viewState = { target: [x, y, 0], zoom: z }; deck.setProps({ viewState }); });

  // editable forks of the cells and regions layers (created once, then reused)
  say("finding or forking editable layers");
  const layers = (await api<LayerInfo[]>(`layers?store=${STORE}`)).body;
  const editable: Record<string, LayerInfo> = {};
  let forkMs: Record<string, number> = {};
  for (const name of ["cells", "regions"]) {
    const src = layers.find((l) => !l.editable && l.name === name)!;
    let e = layers.find((l) => l.forked_from === src.id);
    if (!e) {
      const tf = performance.now();
      e = (await api<LayerInfo>(`layers/${src.id}/fork`, { method: "POST", body: "{}" })).body;
      forkMs = { ...forkMs, [name]: +(performance.now() - tf).toFixed(0) };
    }
    editable[name] = e;
  }
  const rev0 = (await api<{ revision: number }>(`stores/${STORE}/revision`)).body.revision;
  let revision = rev0;
  const cases: Record<string, unknown>[] = [];

  async function editObject(oid: number, kind: string) {
    const [p, i] = where.get(oid)!;
    const s = p.startIndices![i], e = p.startIndices![i + 1], nv = e - s;
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (let k = s; k < e; k++) {
      const x = p.positions[2 * k], y = p.positions[2 * k + 1];
      x0 = Math.min(x0, x); x1 = Math.max(x1, x); y0 = Math.min(y0, y); y1 = Math.max(y1, y);
    }
    const zoom = Math.log2(Math.min(innerWidth / (x1 - x0), innerHeight / (y1 - y0)) * 0.6);
    await setView((x0 + x1) / 2, (y0 + y1) / 2, zoom);

    // pick it in the bulk layer, at a vertex nudged towards the centre
    let cx = 0, cy = 0;
    for (let k = s; k < e; k++) { cx += p.positions[2 * k]; cy += p.positions[2 * k + 1]; }
    cx /= nv; cy /= nv;
    const [px, py] = project(cx, cy);
    const r = canvas.getBoundingClientRect();
    const tp = performance.now();
    const pick = deck.pickObject({ x: px - r.left, y: py - r.top });
    const pickMs = performance.now() - tp;
    const pickedId = pick?.layer ? parts.find((q) => q.id === pick.layer!.id)!.objectIds[pick.index] : null;

    // check out
    t0 = performance.now();
    const fc = await api<FC>(`layers/${editable[p.parent].id}/features?ids=${oid}`);
    const tFetch = performance.now() - t0;
    revision = fc.body.revision ?? revision;
    setHidden(oid, true);
    editData = { type: "FeatureCollection", features: fc.body.features };
    editMode = ModifyMode; selected = [0];
    await renderedAfter(setLayers);
    const checkoutMs = performance.now() - t0;

    // drag one vertex by 10 % of the bbox, one pointermove per frame
    const ring = editData.features[0].geometry.coordinates[0];
    const vi = Math.floor(ring.length / 3);
    const [vx, vy] = ring[vi];
    const dx = (x1 - x0) * 0.1, dy = (y1 - y0) * 0.1;
    const [sx, sy] = project(vx, vy);
    fire("pointermove", sx, sy, 0); await nextFrame(); await nextFrame();
    fire("pointerdown", sx, sy, 1); await nextFrame();
    renderMs.length = 0;
    const dts: number[] = [];
    let last = await nextFrame();
    const steps = 30;
    for (let k = 1; k <= steps; k++) {
      const [mx, my] = project(vx + (dx * k) / steps, vy + (dy * k) / steps);
      fire("pointermove", mx, my, 1);
      const now = await nextFrame();
      dts.push(now - last); last = now;
    }
    const [ex, ey] = project(vx + dx, vy + dy);
    fire("pointerup", ex, ey, 0);
    await nextFrame(); await nextFrame();
    const dragRender = [...renderMs];
    // the dragged handle may be a neighbour of vi where vertices are dense:
    // find the vertex that ended up nearest the drop point
    const after = editData.features[0].geometry.coordinates[0];
    let best = -1, bestD = Infinity;
    after.forEach(([ax, ay], k) => { const d = Math.hypot(ax - (vx + dx), ay - (vy + dy)); if (d < bestD) { bestD = d; best = k; } });
    const vertexMoved = bestD < Math.max(1, (x1 - x0) * 0.02);
    const movedIndexOffset = best - vi;

    // commit
    t0 = performance.now();
    const res = await api<{ revision: number; detail?: unknown }>(`stores/${STORE}/commit`, {
      method: "POST", body: JSON.stringify({ base_revision: revision, client: "s5",
        ops: [{ op: "update_geometry", object_id: oid, geometry: editData.features[0].geometry }] }) });
    const tPost = performance.now() - t0;
    if (res.status === 200) revision = res.body.revision;
    committed.push(editData.features[0]);
    editData = { type: "FeatureCollection", features: [] }; selected = [];
    await renderedAfter(setLayers);
    const commitMs = performance.now() - t0;

    const c = { kind, object_id: oid, vertices: nv, zoom: +zoom.toFixed(2), picked_same_object: pickedId === oid,
      pick_ms: +pickMs.toFixed(1), checkout_fetch_ms: +tFetch.toFixed(1), checkout_ms: +checkoutMs.toFixed(1),
      drag_frame_ms: percentiles(dts), drag_render_ms: percentiles(dragRender), vertex_moved: vertexMoved,
      moved_index_offset: movedIndexOffset, commit_detail: res.status === 200 ? null : res.body.detail,
      last_edit_type: lastEdit, commit_status: res.status, commit_post_ms: +tPost.toFixed(1),
      commit_ms: +commitMs.toFixed(1) };
    cases.push(c);
    log.push(`${kind} ${oid} nv=${nv} checkout ${c.checkout_ms} commit ${c.commit_ms} drag p95 ${c.drag_frame_ms.p95} moved ${vertexMoved}`);
    say(log.slice(-8).join("\n"));
  }

  // one untimed warm-up edit (layer creation, shader compiles), then 20
  // random cells and the 5 regions with the most vertices
  let rng = 4242;
  const rand = () => ((rng = (rng * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff);
  const cellParts = parts.filter((p) => p.parent === "cells");
  await editObject(cellParts[1].objectIds[7], "warmup");
  for (let k = 0; k < 20; k++) {
    const p = cellParts[Math.floor(rand() * cellParts.length)];
    await editObject(p.objectIds[Math.floor(rand() * p.count)], "cell");
  }
  const regions: [number, number][] = [];
  for (const p of parts.filter((q) => q.parent === "regions"))
    for (let i = 0; i < p.count; i++) regions.push([p.startIndices![i + 1] - p.startIndices![i], p.objectIds[i]]);
  regions.sort((a, b) => b[0] - a[0]);
  for (const [, oid] of regions.slice(0, 5)) await editObject(oid, "region");

  // draw a new polygon (8 clicks + click on the first vertex) and commit it
  const cellGroup = editable.cells.groups[0].id;
  await setView(W / 2, H / 2, 2);
  editData = { type: "FeatureCollection", features: [] }; editMode = DrawPolygonMode; selected = [];
  await renderedAfter(setLayers);
  const pts: [number, number][] = [];
  for (let k = 0; k < 8; k++) {
    const a = (k / 8) * 2 * Math.PI;
    pts.push([W / 2 + 40 * Math.cos(a), H / 2 + 40 * Math.sin(a)]);
  }
  const drawT0 = performance.now();
  for (const [x, y] of [...pts, pts[0]]) {
    const [sx, sy] = project(x, y);
    fire("pointermove", sx, sy, 0); await nextFrame();
    fire("pointerdown", sx, sy, 1); fire("pointerup", sx, sy, 0);
    canvas.dispatchEvent(new MouseEvent("click", { bubbles: true, clientX: sx, clientY: sy, detail: 1 }));
    await nextFrame();
  }
  const drawMs = performance.now() - drawT0;
  const drawn = editData.features[0];
  let draw: Record<string, unknown> = { features: editData.features.length, last_edit_type: lastEdit, draw_ms: +drawMs.toFixed(1) };
  if (drawn) {
    t0 = performance.now();
    const res = await api<{ revision: number; created: number[] }>(`stores/${STORE}/commit`, {
      method: "POST", body: JSON.stringify({ base_revision: revision, client: "s5",
        ops: [{ op: "create", group_id: cellGroup, geometry: drawn.geometry }] }) });
    committed.push(drawn);
    editData = { type: "FeatureCollection", features: [] };
    await renderedAfter(setLayers);
    draw = { ...draw, vertices: drawn.geometry.coordinates[0].length, commit_status: res.status,
             created: res.body.created, commit_ms: +(performance.now() - t0).toFixed(1) };
    if (res.status === 200) revision = res.body.revision;
  }

  // stale commit must be refused
  const stale = await api<{ current_revision: number }>(`stores/${STORE}/commit`, { method: "POST",
    body: JSON.stringify({ base_revision: rev0, ops: [] }) });

  const pick = (k: string) => cases.filter((c) => c.kind === k);
  const result = {
    spike: "S5", bench: "edit", set: SET, api: "v1", fork_ms: forkMs, first_frame_ms: +firstFrame.toFixed(1),
    bulk: { cells: cellParts.reduce((a, p) => a + p.count, 0), parts: parts.length },
    summary: Object.fromEntries(["cell", "region"].map((k) => [k, {
      checkout_ms: percentiles(pick(k).map((c) => c.checkout_ms as number)),
      commit_ms: percentiles(pick(k).map((c) => c.commit_ms as number)),
      drag_frame_p95_ms: percentiles(pick(k).map((c) => (c.drag_frame_ms as Record<string, number>).p95)),
      drag_render_p95_ms: percentiles(pick(k).map((c) => (c.drag_render_ms as Record<string, number>).p95)),
      vertex_moved: pick(k).filter((c) => c.vertex_moved).length, picked_same: pick(k).filter((c) => c.picked_same_object).length,
      n: pick(k).length }])),
    draw, stale_commit_status: stale.status, revision_start: rev0, revision_end: revision,
    cases, environment: environment(gl), deck_version: (await import("@deck.gl/core")).VERSION,
  };
  const saved = await postResults("s5-edit", result);
  say(`done -> ${saved}\n` + log.slice(-6).join("\n"));
  (window as unknown as { __benchDone?: unknown }).__benchDone = result;
}
