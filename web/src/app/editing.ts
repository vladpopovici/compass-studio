// Annotation editing (milestone 7, decision D8), chosen by the owner:
// - every action commits at once (one batch per action, against the
//   store's current revision); nothing is ever unsaved;
// - undo and redo commit the logged inverse; the stacks are rebuilt from
//   the server log on load (commits carry this browser's client label), so
//   they survive reloads;
// - editing a read-only layer asks to fork it first (or to use an existing
//   fork); the fork keeps object ids, so the selection carries over;
// - tools: select, modify (vertices and moving), polygon, freehand,
//   rectangle, ellipse, point, line.
//
// Changes by anyone (this client included) reach the view through the
// sidecar's revision: on each new revision the log since the last one names
// the objects touched; their current GeoJSON is fetched and drawn by the
// edit overlay, and they are hidden in the bulk layers.

import { useCallback, useEffect, useMemo, useRef, useState, type Dispatch, type SetStateAction } from "react";
import { authHeaders, getJSON, postJSON, type LayerInfo } from "@/api";
import { DEFAULT_ANN_STYLE, type AnnSource, type AnnStyle, type ObjectPick } from "@/viewer/annstyle";
import { featurePick, OverlaySource, DRAW_TOOLS, popVertex, type EditEvent, type EditFeature, type Tool } from "@/viewer/edits";
import { clean, ellipseInBox, problem, simplifyRing, type Geometry, type Pos } from "@/viewer/geometry";
import type { useAnnotations } from "./annotations";
import { objectKey } from "./Inspector";

type Op = { op: string; object_id?: number; [k: string]: unknown };
interface LogEntry { revision: number; ts: number; client: string | null; ops: Op[]; inverse: Op[] }
interface Override { layerId: string; feature: EditFeature }
interface Step { store: string; rev: number }
interface RedoStep extends Step { undoRev: number }

export interface ForkAsk { layer: LayerInfo; existing: LayerInfo | null; answer: (a: "existing" | "new" | null) => void }
export interface Message { text: string; error: boolean; id: number }
export interface Checkout { key: string; layerId: string; storeId: string; features: EditFeature[]; committed: EditFeature[] }

/** objects checked out at once by the modify tool */
const MAX_CHECKOUT = 50;

const SESSION = (() => {
  try {
    let v = localStorage.getItem("cs.session");
    if (!v) localStorage.setItem("cs.session", (v = Math.random().toString(36).slice(2, 10)));
    return v;
  } catch {
    return Math.random().toString(36).slice(2, 10);
  }
})();
/** client label of this browser's commits; undo and redo add `:undo:<rev>` / `:redo:<rev>`;
 *  "Discard changes" undoes several revisions in one batch, `:undo:<rev>,<rev>,…` */
export const CLIENT = `compass-studio:${SESSION}`;

function lastIndex<T>(a: T[], f: (x: T) => boolean): number {
  for (let i = a.length - 1; i >= 0; i--) if (f(a[i])) return i;
  return -1;
}

async function fetchLog(store: string, since: number): Promise<LogEntry[]> {
  const out: LogEntry[] = [];
  for (;;) {
    const r = await getJSON<{ entries: LogEntry[] }>(`stores/${encodeURIComponent(store)}/log?since=${since}&limit=1000`);
    out.push(...r.entries);
    if (r.entries.length < 1000) return out;
    since = r.entries[r.entries.length - 1].revision;
  }
}

async function logEntry(store: string, rev: number): Promise<LogEntry> {
  const r = await getJSON<{ entries: LogEntry[] }>(`stores/${encodeURIComponent(store)}/log?since=${rev - 1}&limit=1`);
  if (r.entries[0]?.revision !== rev) throw new Error(`revision ${rev} is not in the log`);
  return r.entries[0];
}

type FeatureCollection = { features: { id: number; geometry: Geometry; properties: { group: number } }[] };

async function fetchFeatures(layerId: string, ids: number[]): Promise<EditFeature[]> {
  const out: EditFeature[] = [];
  for (let i = 0; i < ids.length; i += 1000) {
    const fc = await getJSON<FeatureCollection>(`layers/${encodeURIComponent(layerId)}/features?ids=${ids.slice(i, i + 1000).join(",")}`);
    for (const f of fc.features) out.push({ id: f.id, group: f.properties.group, geometry: f.geometry });
  }
  return out;
}

export function useEditing(opts: {
  imageId: string | null;
  ann: ReturnType<typeof useAnnotations>;
  selection: ObjectPick[];
  setSelection: Dispatch<SetStateAction<ObjectPick[]>>;
  invalidate: (keys: string[]) => void;
  zoom: number;
}) {
  const { imageId, ann, selection, setSelection, invalidate, zoom } = opts;
  const [tool, setTool] = useState<Tool>("select");
  const [target, setTarget] = useState<{ layerId: string; group: number } | null>(null);
  const [version, setVersion] = useState(0);
  const [stacks, setStacks] = useState({ undo: 0, redo: 0, base: 0 });
  const [checkout, setCheckout] = useState<Checkout | null>(null);
  const [pending, setPending] = useState<EditFeature[]>([]);
  const [forkAsk, setForkAsk] = useState<ForkAsk | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<Message | null>(null);

  /** store -> object id -> current state (null: deleted), for objects changed since the bulk data loaded */
  const overrides = useRef(new Map<string, Map<number, Override | null>>());
  /** last revision synced into `overrides`, per store */
  const applied = useRef<Record<string, number>>({});
  /** latest revision known (commits, events), per store */
  const known = useRef<Record<string, number>>({});
  const undoStack = useRef<Step[]>([]);
  const redoStack = useRef<RedoStep[]>([]);
  /** undo stack depth when the slide was opened: "Discard changes" reverts what is above it */
  const sessionBase = useRef(0);
  const overlays = useRef(new Map<string, OverlaySource>());
  const chains = useRef(new Map<string, Promise<void>>());
  const forks = useRef(new Map<string, Promise<LayerInfo | null>>());
  const refreshTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  /** round trips in ms, for measurements: commit to view updated, selection to editor ready */
  const timing = useRef({ commits: [] as number[], checkouts: [] as number[] });

  // `ann` is a new object on every render: callbacks use it through a ref so they stay stable
  const annRef = useRef(ann);
  annRef.current = ann;
  const layers = useMemo(() => ann.layers.map((l) => l.info), [ann.layers]);
  const layersRef = useRef(layers);
  layersRef.current = layers;
  const storesRef = useRef(ann.stores);
  storesRef.current = ann.stores;
  const selectionRef = useRef(selection);
  selectionRef.current = selection;
  const stylesRef = useRef(ann.layers);
  stylesRef.current = ann.layers;
  const zoomRef = useRef(zoom);
  zoomRef.current = zoom;

  const say = useCallback((text: string, error = false) => setMessage({ text, error, id: Date.now() }), []);
  useEffect(() => {
    if (!message) return;
    const t = setTimeout(() => setMessage(null), message.error ? 6000 : 3000);
    return () => clearTimeout(t);
  }, [message]);
  const bumpStacks = () => {
    sessionBase.current = Math.min(sessionBase.current, undoStack.current.length);
    setStacks({ undo: undoStack.current.length, redo: redoStack.current.length, base: sessionBase.current });
  };

  // -- per image -------------------------------------------------------------

  useEffect(() => {
    overrides.current = new Map();
    applied.current = {};
    undoStack.current = [];
    redoStack.current = [];
    overlays.current = new Map();
    chains.current = new Map();
    setCheckout(null);
    setPending([]);
    setTarget(null);
    setVersion((v) => v + 1);
    bumpStacks();
  }, [imageId]);

  const storeKey = ann.stores.filter((s) => s.editable).map((s) => s.id).join(",");
  useEffect(() => {
    // revisions the bulk data is at (or later: re-applying a change is harmless)
    for (const s of storesRef.current)
      if (s.editable && applied.current[s.id] === undefined) applied.current[s.id] = s.revision;
    // undo and redo stacks from this browser's commits in the logs
    let cancelled = false;
    (async () => {
      const all: (LogEntry & { store: string })[] = [];
      for (const s of storesRef.current.filter((x) => x.editable))
        for (const e of await fetchLog(s.id, 0)) if (e.client?.startsWith(CLIENT)) all.push({ ...e, store: s.id });
      if (cancelled) return;
      all.sort((a, b) => a.ts - b.ts);
      const undo: Step[] = [], redo: RedoStep[] = [];
      for (const e of all) {
        const m = e.client!.slice(CLIENT.length).match(/^:(undo|redo):([\d,]+)$/);
        if (!m) {
          undo.push({ store: e.store, rev: e.revision });
          redo.length = 0;
        } else if (m[1] === "undo") {
          // one revision (undo) or several in one batch (discard): one redo step
          let found = false;
          for (const r of m[2].split(",").map(Number)) {
            const i = lastIndex(undo, (x) => x.store === e.store && x.rev === r);
            if (i >= 0) { undo.splice(i, 1); found = true; }
          }
          if (found) redo.push({ store: e.store, rev: +m[2].split(",")[0], undoRev: e.revision });
        } else {
          const i = lastIndex(redo, (x) => x.store === e.store && x.undoRev === +m[2]);
          if (i >= 0) redo.splice(i, 1);
          undo.push({ store: e.store, rev: e.revision });
        }
      }
      undoStack.current = undo;
      redoStack.current = redo;
      sessionBase.current = undo.length;
      bumpStacks();
    })().catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [storeKey]);

  // -- sync from the log -------------------------------------------------------

  const scheduleRefresh = useCallback((now = false) => {
    if (refreshTimer.current) clearTimeout(refreshTimer.current);
    refreshTimer.current = setTimeout(() => void annRef.current.refresh(), now ? 0 : 300);
  }, []);

  const doSync = useCallback(async (store: string) => {
    const since = applied.current[store];
    if (since === undefined) return;
    const entries = await fetchLog(store, since);
    if (!entries.length) return;
    let structural = false;
    const geo = new Set<number>(), attrs = new Set<number>();
    for (const e of entries) {
      const n = e.ops.length;
      e.ops.forEach((op, i) => {
        if (op.op === "fork_layer" || op.op === "create_layer") structural = true;
        const id = op.object_id ?? (op.op === "create" ? e.inverse[n - 1 - i]?.object_id : undefined);
        if (id === undefined) return;
        (op.op === "update_attributes" ? attrs : geo).add(id);
      });
    }
    let ls = layersRef.current;
    if (structural) ls = await annRef.current.refresh();
    const editable = ls.filter((l) => l.store_id === store && l.editable);
    if (geo.size) {
      const ids = [...geo], found = new Map<number, Override>();
      for (const l of editable)
        for (const f of await fetchFeatures(l.id, ids)) found.set(f.id, { layerId: l.id, feature: f });
      const m = overrides.current.get(store) ?? new Map<number, Override | null>();
      for (const id of ids) m.set(id, found.get(id) ?? null);
      overrides.current.set(store, m);
    }
    const touched = [...geo, ...attrs];
    if (touched.length) invalidate(editable.flatMap((l) => touched.map((id) => objectKey({ layerId: l.id, objectId: id }))));
    applied.current[store] = Math.max(since, entries[entries.length - 1].revision);
    known.current[store] = Math.max(known.current[store] ?? 0, applied.current[store]);
    setVersion((v) => v + 1);
    if (touched.length) scheduleRefresh();
  }, [invalidate, scheduleRefresh]);

  /** Bring a store's changes into the view; calls for one store run one after another. */
  const sync = useCallback((store: string): Promise<void> => {
    const next = (chains.current.get(store) ?? Promise.resolve()).then(() => doSync(store)).catch((e: Error) => {
      say(`Could not read changes: ${e.message}`, true);
    });
    chains.current.set(store, next);
    return next;
  }, [doSync, say]);

  // server-sent events: revisions from this server and any other writer
  useEffect(() => {
    const ac = new AbortController();
    const onRevision = (store: string, rev: number) => {
      known.current[store] = Math.max(known.current[store] ?? 0, rev);
      const a = applied.current[store];
      if (a !== undefined && rev > a) void sync(store);
    };
    (async () => {
      while (!ac.signal.aborted) {
        try {
          const r = await fetch("api/v1/events", { headers: authHeaders, signal: ac.signal });
          const reader = r.body!.pipeThrough(new TextDecoderStream()).getReader();
          let buf = "";
          for (;;) {
            const { value, done } = await reader.read();
            if (done) break;
            buf += value;
            let i;
            while ((i = buf.indexOf("\n\n")) >= 0) {
              const block = buf.slice(0, i);
              buf = buf.slice(i + 2);
              const ev = block.match(/^event: (.*)$/m)?.[1], data = block.match(/^data: (.*)$/m)?.[1];
              if (!ev || !data) continue;
              const d = JSON.parse(data);
              if (ev === "revision") onRevision(d.store_id, d.revision);
              else if (ev === "hello") for (const [s, rev] of Object.entries(d.revisions as Record<string, number>)) onRevision(s, rev);
            }
          }
        } catch {
          // reconnect below
        }
        if (!ac.signal.aborted) await new Promise((r) => setTimeout(r, 2000));
      }
    })();
    return () => ac.abort();
  }, [sync]);

  // -- commits, undo, redo ------------------------------------------------------

  const commit = useCallback(async (store: string, ops: Op[], client = CLIENT) => {
    const t0 = performance.now();
    let base = Math.max(known.current[store] ?? 0, applied.current[store] ?? 0);
    for (let attempt = 0; attempt < 3; attempt++) {
      const r = await postJSON<{ revision: number; created: number[]; current_revision: number; detail: string }>(
        `stores/${encodeURIComponent(store)}/commit`, { base_revision: base, ops, client });
      if (r.status === 409) {
        base = r.data.current_revision;
        continue;
      }
      if (!r.ok) throw new Error(String(r.data.detail ?? r.status).replace(/^invalid operations: /, ""));
      known.current[store] = Math.max(known.current[store] ?? 0, r.data.revision);
      await sync(store);
      timing.current.commits.push(performance.now() - t0);
      return r.data;
    }
    throw new Error("the store keeps changing; try again");
  }, [sync]);

  /** A user action: commit, record for undo. Errors are reported; returns null then. */
  const act = useCallback(async (store: string, ops: Op[]) => {
    try {
      const r = await commit(store, ops);
      undoStack.current.push({ store, rev: r.revision });
      redoStack.current = [];
      bumpStacks();
      return r;
    } catch (e) {
      say(`Not saved: ${(e as Error).message}`, true);
      return null;
    }
  }, [commit, say]);

  const undo = useCallback(async () => {
    const s = undoStack.current.pop();
    if (!s) return;
    bumpStacks();
    try {
      const e = await logEntry(s.store, s.rev);
      const r = await commit(s.store, e.inverse, `${CLIENT}:undo:${s.rev}`);
      redoStack.current.push({ ...s, undoRev: r.revision });
      say("Undone");
    } catch (e) {
      say(`Cannot undo: ${(e as Error).message}`, true);
    }
    bumpStacks();
  }, [commit, say]);

  const redo = useCallback(async () => {
    const s = redoStack.current.pop();
    if (!s) return;
    bumpStacks();
    try {
      const e = await logEntry(s.store, s.undoRev);
      const r = await commit(s.store, e.inverse, `${CLIENT}:redo:${s.undoRev}`);
      undoStack.current.push({ store: s.store, rev: r.revision });
      say("Redone");
    } catch (e) {
      say(`Cannot redo: ${(e as Error).message}`, true);
    }
    bumpStacks();
  }, [commit, say]);

  /** Revert every edit of this browser since the slide was opened: one batch per store, one redo step each. */
  const discard = useCallback(async () => {
    const steps = undoStack.current.slice(sessionBase.current);
    if (!steps.length) return;
    const byStore = new Map<string, number[]>();
    for (const s of steps) byStore.set(s.store, [...(byStore.get(s.store) ?? []), s.rev]);
    let n = 0;
    for (const [store, revs] of byStore) {
      try {
        // inverses newest first, as undoing them one by one would
        const entries = await Promise.all(revs.map((r) => logEntry(store, r)));
        const ops = entries.reverse().flatMap((e) => e.inverse);
        const r = await commit(store, ops, `${CLIENT}:undo:${[...revs].reverse().join(",")}`);
        const drop = new Set(revs);
        undoStack.current = undoStack.current.filter((x) => x.store !== store || !drop.has(x.rev));
        redoStack.current.push({ store, rev: revs[revs.length - 1], undoRev: r.revision });
        n += revs.length;
      } catch (e) {
        say(`Cannot discard: ${(e as Error).message}`, true);
      }
    }
    bumpStacks();
    if (n) say(`Discarded ${n} change${n === 1 ? "" : "s"}`);
  }, [commit, say]);

  /** Make an editable layer's current state its save point ("Save edits"). */
  const saveLayer = useCallback(async (layerId: string) => {
    const r = await postJSON<LayerInfo & { detail?: string }>(`layers/${encodeURIComponent(layerId)}/save`, {});
    if (!r.ok) {
      say(`Could not save: ${r.data.detail ?? r.status}`, true);
      return;
    }
    await annRef.current.refresh();
    say(`Saved ${r.data.name}`);
  }, [say]);

  /** Revert an editable layer to its save point ("Discard edits"): one undoable step. */
  const discardLayer = useCallback(async (layerId: string) => {
    const info = layersRef.current.find((l) => l.id === layerId);
    if (!info) return;
    const r = await postJSON<{ revision: number; reverted: number[]; detail?: string }>(
      `layers/${encodeURIComponent(layerId)}/discard`, { client: `${CLIENT}:undo:{reverted}` });
    if (!r.ok) {
      say(`Could not discard: ${r.data.detail ?? r.status}`, true);
      return;
    }
    if (r.data.reverted.length) {
      // recorded like "Discard changes": an undo of those revisions, one Redo brings them back
      const drop = new Set(r.data.reverted);
      undoStack.current = undoStack.current.filter((x) => x.store !== info.store_id || !drop.has(x.rev));
      redoStack.current.push({ store: info.store_id, rev: r.data.reverted[r.data.reverted.length - 1], undoRev: r.data.revision });
      bumpStacks();
      known.current[info.store_id] = Math.max(known.current[info.store_id] ?? 0, r.data.revision);
      await sync(info.store_id);
    }
    await annRef.current.refresh();
    say(`Discarded the edits of ${info.name}`);
  }, [say, sync]);

  // -- read-only layers: fork on first edit ----------------------------------------

  const setStyleOf = ann.setStyles;
  /** The editable layer to apply an edit of `layerId` to (asking to fork a read-only one), or null. */
  const ensureEditable = useCallback((layerId: string): Promise<LayerInfo | null> => {
    const info = layersRef.current.find((l) => l.id === layerId);
    if (!info) return Promise.resolve(null);
    if (info.editable) return Promise.resolve(info);
    const inflight = forks.current.get(layerId);
    if (inflight) return inflight;
    const p = (async () => {
      const store = storesRef.current.find((s) => s.id === info.store_id);
      if (!store?.editable) {
        say("This store cannot be edited (the server runs with --read-only).", true);
        return null;
      }
      const existing = layersRef.current.find((l) => l.editable && l.forked_from === info.id) ?? null;
      const choice = await new Promise<"existing" | "new" | null>((answer) => setForkAsk({ layer: info, existing, answer }));
      setForkAsk(null);
      if (!choice) return null;
      let made: LayerInfo | null = existing;
      if (choice === "new") {
        setBusy(`Making an editable copy of ${info.name}…`);
        try {
          // layer names are unique within a store's curation database
          const taken = new Set(layersRef.current.filter((l) => l.store_id === info.store_id).map((l) => l.name));
          taken.delete(info.name); // the store layer itself is not in the curation database
          let name = info.name;
          for (let i = 2; taken.has(name); i++) name = `${info.name} ${i}`;
          const r = await postJSON<LayerInfo & { detail?: string }>(`layers/${encodeURIComponent(info.id)}/fork`, { name });
          if (!r.ok) throw new Error(r.data.detail ?? String(r.status));
          made = r.data;
        } catch (e) {
          say(`Could not copy ${info.name}: ${(e as Error).message}`, true);
          return null;
        } finally {
          setBusy(null);
        }
      }
      // the copy takes the original's style and its place in the list
      setStyleOf((o) => ({ ...o, [made!.id]: { ...(o[info.id] ?? DEFAULT_ANN_STYLE), visible: true } }));
      const ls = await annRef.current.refresh();
      const fork = ls.find((l) => l.id === made!.id) ?? made!;
      setSelection((sel) => sel.map((p) => (p.layerId === info.id ? { ...p, layerId: fork.id } : p)));
      setTarget((t) => (t?.layerId === info.id ? { ...t, layerId: fork.id } : t));
      say(`Editing ${fork.name} (editable)`);
      return fork;
    })().finally(() => forks.current.delete(layerId));
    forks.current.set(layerId, p);
    return p;
  }, [say, setSelection, setStyleOf]);

  const createLayer = useCallback(async (storeId: string, name: string, groups: string[]) => {
    const r = await postJSON<LayerInfo & { detail?: string }>(`stores/${encodeURIComponent(storeId)}/layers`, { name, groups });
    if (!r.ok) {
      say(`Could not create the layer: ${r.data.detail ?? r.status}`, true);
      return null;
    }
    await annRef.current.refresh();
    setTarget({ layerId: r.data.id, group: 0 });
    say(`Created ${r.data.name}`);
    return r.data;
  }, [say]);

  // -- drawing ------------------------------------------------------------------

  const targetInfo = layers.find((l) => l.id === target?.layerId) ?? null;
  // default target: the first editable layer, else the first layer with groups
  useEffect(() => {
    if (targetInfo || !layers.length) return;
    const l = layers.find((x) => x.editable && x.groups.length) ?? layers.find((x) => x.groups.length);
    if (l) setTarget({ layerId: l.id, group: 0 });
  }, [layers, targetInfo]);

  const finishDrawing = useCallback(async (drawn: Geometry, t: Tool) => {
    let g = clean(drawn);
    const px = 1 / 2 ** zoomRef.current; // level-0 px per screen px
    if (g.type === "Polygon" && t === "lasso") g = { type: "Polygon", coordinates: [simplifyRing(g.coordinates[0], Math.max(0.25, px))] };
    if (g.type === "Polygon" && t === "ellipse") g = { type: "Polygon", coordinates: [ellipseInBox(g.coordinates[0] as Pos[])] };
    const why = problem(g);
    if (why) {
      say(`Not saved: ${why}.`, true);
      return;
    }
    const tg = target;
    if (!tg) {
      say("Choose a layer to draw into.", true);
      return;
    }
    const layer = await ensureEditable(tg.layerId);
    if (!layer) return;
    const group = layer.groups[tg.group] ?? layer.groups[0];
    if (!group) {
      say(`${layer.name} has no groups to draw into.`, true);
      return;
    }
    const temp: EditFeature = { id: -Date.now(), group: group.index, geometry: g };
    setPending((p) => [...p, temp]);
    const r = await act(layer.store_id, [{ op: "create", group_id: group.id, geometry: g }]);
    setPending((p) => p.filter((f) => f !== temp));
    if (r?.created[0] !== undefined) setSelection([featurePick(layer.id, { id: r.created[0], group: group.index, geometry: g })]);
  }, [target, ensureEditable, act, say, setSelection]);

  // -- modify: check-out of the selection ------------------------------------------

  const selKey = selection.map(objectKey).join(";");
  // "Into" follows the selection: the layer and group of the last selected object
  useEffect(() => {
    const p = selectionRef.current[selectionRef.current.length - 1];
    if (!p || !layersRef.current.some((l) => l.id === p.layerId && l.groups.length)) return;
    setTarget((t) => (t?.layerId === p.layerId && t.group === p.group ? t : { layerId: p.layerId, group: p.group }));
  }, [selKey, selection]);
  useEffect(() => {
    if (tool !== "modify" || !selectionRef.current.length) {
      setCheckout(null);
      return;
    }
    const sel = selectionRef.current;
    const layerId = sel[0].layerId;
    const ids = sel.filter((p) => p.layerId === layerId).slice(0, MAX_CHECKOUT).map((p) => p.objectId);
    const key = `${layerId}|${ids.join(",")}`;
    let cancelled = false;
    const t0 = performance.now();
    (async () => {
      const layer = await ensureEditable(layerId);
      if (cancelled) return;
      if (!layer) {
        setTool("select");
        return;
      }
      if (layer.id !== layerId) return; // the selection moves to the fork; this runs again
      const features = await fetchFeatures(layer.id, ids);
      if (cancelled) return;
      setCheckout((c) => (c?.key === key ? c : { key, layerId, storeId: layer.store_id, features, committed: features }));
      if (layer.editable && layer.id === layerId) timing.current.checkouts.push(performance.now() - t0);
    })().catch((e: Error) => say(`Cannot edit: ${e.message}`, true));
    return () => {
      cancelled = true;
    };
  }, [tool, selKey, ensureEditable, say]);

  const checkoutRef = useRef(checkout);
  checkoutRef.current = checkout;

  const onEdit = useCallback((e: EditEvent) => {
    const t = toolRef.current;
    if (DRAW_TOOLS.has(t)) {
      if (e.editType === "addFeature") {
        const f = e.updatedData.features[e.updatedData.features.length - 1];
        if (f) void finishDrawing(f.geometry, t);
      }
      return;
    }
    const c = checkoutRef.current;
    if (t !== "modify" || !c) return;
    const features = c.features.map((f, i) => ({ ...f, geometry: e.updatedData.features[i]?.geometry ?? f.geometry }));
    const next = { ...c, features };
    setCheckout(next);
    if (!["finishMovePosition", "addPosition", "removePosition", "translated"].includes(e.editType)) return;
    const changed = e.editContext?.featureIndexes ?? features.map((_, i) => i);
    const ops: Op[] = [];
    for (const i of changed) {
      const g = clean(features[i].geometry), why = problem(g);
      if (why) {
        say(`Not saved: ${why}.`, true);
        setCheckout({ ...next, features: next.features.map((f, j) => (j === i ? next.committed[j] : f)) });
        return;
      }
      ops.push({ op: "update_geometry", object_id: features[i].id, geometry: g });
    }
    void act(c.storeId, ops).then((r) => {
      setCheckout((cur) => {
        if (!cur || cur.key !== c.key) return cur;
        if (!r) return { ...cur, features: cur.committed }; // refused: back to the last saved shape
        const committed = cur.committed.map((f, j) => (changed.includes(j) ? features[j] : f));
        return { ...cur, committed };
      });
    });
  }, [finishDrawing, act, say]);

  const toolRef = useRef(tool);
  toolRef.current = tool;

  // -- other actions on the selection -------------------------------------------------

  /** Apply `make` to the selected objects of each layer, forking read-only layers first. */
  const onSelection = useCallback(async (picks: ObjectPick[], make: (layer: LayerInfo, ids: number[]) => Op[]) => {
    const byLayer = new Map<string, number[]>();
    for (const p of picks) byLayer.set(p.layerId, [...(byLayer.get(p.layerId) ?? []), p.objectId]);
    for (const [layerId, ids] of byLayer) {
      const layer = await ensureEditable(layerId);
      if (!layer) continue;
      const ops = make(layer, ids);
      if (ops.length) await act(layer.store_id, ops);
    }
  }, [ensureEditable, act]);

  const deleteSelected = useCallback(async () => {
    const sel = selectionRef.current;
    if (!sel.length) return;
    await onSelection(sel, (_, ids) => ids.map((id) => ({ op: "delete", object_id: id })));
  }, [onSelection]);

  const setGroup = useCallback((picks: ObjectPick[], groupIndex: number) => onSelection(picks, (layer, ids) => {
    const g = layer.groups[groupIndex];
    return g ? ids.map((id) => ({ op: "move", object_id: id, group_id: g.id })) : [];
  }), [onSelection]);

  const setValues = useCallback((picks: ObjectPick[], values: Record<string, number | null>) =>
    onSelection(picks, (_, ids) => ids.map((id) => ({ op: "update_attributes", object_id: id, values }))), [onSelection]);

  // -- what the viewer draws -------------------------------------------------------------

  const hiddenSeq = useRef(0);
  const overlayEntries = useMemo(() => {
    const out: { source: AnnSource; style: AnnStyle }[] = [];
    const seq = ++hiddenSeq.current;
    for (const l of stylesRef.current) {
      if (!l.info.editable) continue;
      const m = overrides.current.get(l.info.store_id);
      const out_ids = new Set<number>(m ? m.keys() : []);
      const busyIds = checkout?.layerId === l.info.id ? new Set(checkout.features.map((f) => f.id)) : new Set<number>();
      for (const id of busyIds) out_ids.add(id);
      const s = ann.sources.current.get(l.info.id);
      s?.points?.setHidden(out_ids, seq);
      s?.polygons?.setHidden(out_ids, seq);
      s?.lines?.setHidden(out_ids, seq);
      let ov = overlays.current.get(l.info.id);
      if (!ov) overlays.current.set(l.info.id, (ov = new OverlaySource(l.info)));
      const feats: EditFeature[] = [];
      if (m) for (const o of m.values()) if (o && o.layerId === l.info.id && !busyIds.has(o.feature.id)) feats.push(o.feature);
      ov.setFeatures(feats);
      out.push({ source: ov, style: l.style });
    }
    return out;
    // ann.layers: styles and new layers; version: overrides; checkout: hidden while edited
  }, [ann.layers, ann.sources, version, checkout?.key, checkout?.layerId]); // eslint-disable-line react-hooks/exhaustive-deps

  // the selection follows changes: new shapes, groups; deleted objects leave it
  useEffect(() => {
    setSelection((sel) => {
      let changed = false;
      const out: ObjectPick[] = [];
      for (const p of sel) {
        const l = layersRef.current.find((x) => x.id === p.layerId);
        const o = l?.editable ? overrides.current.get(l.store_id)?.get(p.objectId) : undefined;
        if (o === undefined) out.push(p);
        else {
          changed = true;
          if (o && o.layerId === p.layerId) out.push(featurePick(p.layerId, o.feature));
        }
      }
      return changed ? out : sel;
    });
    // objects being edited take group changes (their geometry is the editor's)
    setCheckout((c) => {
      if (!c) return c;
      const m = overrides.current.get(c.storeId);
      let changed = false;
      const regroup = (f: EditFeature) => {
        const o = m?.get(f.id);
        if (!o || o.feature.group === f.group) return f;
        changed = true;
        return { ...f, group: o.feature.group };
      };
      const features = c.features.map(regroup), committed = c.committed.map(regroup);
      return changed ? { ...c, features, committed } : c;
    });
  }, [version, setSelection]);

  /** Backspace while drawing a polygon or line: drop the last vertex (`drawTick` redraws the guide). */
  const [drawTick, setDrawTick] = useState(0);
  const removeLastVertex = useCallback(() => {
    if (!popVertex(toolRef.current)) return false;
    setDrawTick((t) => t + 1);
    return true;
  }, []);

  return {
    tool, setTool, target, setTarget, targetInfo, checkout, pending, onEdit, overlayEntries,
    undo, redo, canUndo: stacks.undo > 0, canRedo: stacks.redo > 0,
    discard, sessionEdits: stacks.undo - stacks.base, saveLayer, discardLayer,
    deleteSelected, setGroup, setValues, createLayer, ensureEditable,
    forkAsk, busy, message, say, removeLastVertex, drawTick, timing,
  };
}
