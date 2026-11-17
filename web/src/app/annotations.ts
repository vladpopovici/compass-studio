// Annotation stores and layers of the current image, their sources (points
// and polylines fetched whole, polygons by tile) and the attributes of
// selected objects (fetched lazily, cached per session).
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { getJSON, type LayerInfo, type Schemas } from "@/api";
import { DEFAULT_ANN_STYLE, type AnnSource, type AnnStyle, type ObjectPick } from "@/viewer/annstyle";
import { LineSource } from "@/viewer/lines";
import { PointSource } from "@/viewer/points";
import { PolygonSource } from "@/viewer/polygons";
import { objectKey, type ObjectInfo } from "./Inspector";
import type { LayerState, WholeLoad } from "./LayersPanel";

type Store = Schemas["Store"];

interface Sources { points?: PointSource; lines?: LineSource; polygons?: PolygonSource }

export function useAnnotations(imageId: string | null) {
  const [stores, setStores] = useState<Store[]>([]);
  const [layers, setLayers] = useState<LayerInfo[]>([]);
  /** whole-layer loads, keyed `${layer id}|points` and `${layer id}|lines` */
  const [loads, setLoads] = useState<Record<string, WholeLoad>>({});
  const [styles, setStyles] = useState<Record<string, AnnStyle>>({});
  const [error, setError] = useState<string | null>(null);
  const sources = useRef(new Map<string, Sources>());
  const current = useRef<{ imageId: string; ac: AbortController } | null>(null);

  /** Sources for a layer's object types (new ones only: a type can appear after edits). */
  const ensureSources = useCallback((l: LayerInfo, ac: AbortController) => {
    const made = sources.current;
    const s = made.get(l.id) ?? {};
    made.set(l.id, s);
    if (l.counts.polygon && !s.polygons) s.polygons = new PolygonSource(l);
    const whole: ["points" | "lines", PointSource | LineSource][] = [];
    if (l.counts.point && !s.points) whole.push(["points", (s.points = new PointSource(l))]);
    if (l.counts.polyline && !s.lines) whole.push(["lines", (s.lines = new LineSource(l))]);
    for (const [kind, src] of whole) {
      const key = `${l.id}|${kind}`;
      setLoads((o) => ({ ...o, [key]: { state: "loading" } }));
      src.load(ac.signal).then(
        () => setLoads((o) => ({ ...o, [key]: { state: "ready", count: src.count, groupCounts: src.groupCounts() } })),
        (e: Error) => {
          if (!ac.signal.aborted) setLoads((o) => ({ ...o, [key]: { state: "error", message: e.message } }));
        });
    }
  }, []);

  /** Stores and layers of an image, with sources for new layers. */
  const fetchLayers = useCallback(async (id: string, ac: AbortController) => {
    const mine = (await getJSON<Store[]>("stores")).filter((s) => s.aligned_to === id);
    if (ac.signal.aborted) return null;
    const ls = (await Promise.all(mine.map((s) => getJSON<LayerInfo[]>(`layers?store=${encodeURIComponent(s.id)}`)))).flat();
    if (ac.signal.aborted) return null;
    for (const l of ls) ensureSources(l, ac);
    setStores(mine);
    setLayers(ls);
    return ls;
  }, [ensureSources]);

  useEffect(() => {
    setStores([]);
    setLayers([]);
    setLoads({});
    setError(null);
    sources.current = new Map();
    current.current = null;
    if (!imageId) return;
    const ac = new AbortController();
    const made = sources.current;
    current.current = { imageId, ac };
    fetchLayers(imageId, ac).catch((e: Error) => {
      if (!ac.signal.aborted) setError(e.message);
    });
    return () => {
      ac.abort();
      for (const s of made.values()) s.polygons?.destroy();
    };
  }, [imageId, fetchLayers]);

  /** Re-read stores and layers (after forks, new layers and edits: counts, revisions). */
  const refresh = useCallback(async (): Promise<LayerInfo[]> => {
    const c = current.current;
    if (!c) return [];
    return (await fetchLayers(c.imageId, c.ac)) ?? [];
  }, [fetchLayers]);

  const setStyle = useCallback((id: string, s: AnnStyle) => setStyles((o) => ({ ...o, [id]: s })), []);

  // a store layer with an editable copy is not listed: the copy stands in for it
  const listed = useMemo(() => {
    const copied = new Set(layers.map((l) => l.forked_from).filter(Boolean));
    return layers.filter((l) => !copied.has(l.id));
  }, [layers]);
  const layerStates: LayerState[] = useMemo(() => listed.map((info) => ({
    info, style: styles[info.id] ?? DEFAULT_ANN_STYLE,
    points: loads[`${info.id}|points`] ?? null, lines: loads[`${info.id}|lines`] ?? null,
  })), [listed, styles, loads]);
  /** sources to draw with their styles, bottom first: polygons, then lines, then points of every layer */
  const drawn = useMemo(() => {
    const out: { source: AnnSource; style: AnnStyle }[] = [];
    for (const kind of ["polygons", "lines", "points"] as const)
      for (const l of layerStates) {
        const source = sources.current.get(l.info.id)?.[kind];
        if (!source) continue;
        if (kind !== "polygons" && l[kind]?.state !== "ready") continue;
        out.push({ source, style: l.style });
      }
    return out;
  }, [layerStates]);
  return { stores, layers: layerStates, drawn, setStyle, setStyles, error, sources, refresh };
}

/** Attributes of the selected objects, fetched by id in batches of up to 1000 per layer. */
export function useObjects(selection: ObjectPick[]) {
  const [objects, setObjects] = useState<Record<string, ObjectInfo | "error">>({});
  const asked = useRef(new Set<string>());
  const [tick, setTick] = useState(0);
  useEffect(() => {
    const want = new Map<string, number[]>();
    for (const p of selection) {
      const k = objectKey(p);
      if (asked.current.has(k)) continue;
      asked.current.add(k);
      want.set(p.layerId, [...(want.get(p.layerId) ?? []), p.objectId]);
    }
    for (const [layerId, ids] of want) {
      for (let i = 0; i < ids.length; i += 1000) {
        const chunk = ids.slice(i, i + 1000);
        getJSON<{ objects: ObjectInfo[] }>(`layers/${encodeURIComponent(layerId)}/objects?ids=${chunk.join(",")}`).then(
          (r) => setObjects((o) => {
            const n = { ...o };
            for (const obj of r.objects) n[objectKey({ layerId, objectId: obj.object_id })] = obj;
            return n;
          }),
          () => {
            setObjects((o) => {
              const n = { ...o };
              for (const id of chunk) n[objectKey({ layerId, objectId: id })] = "error";
              return n;
            });
            for (const id of chunk) asked.current.delete(objectKey({ layerId, objectId: id }));
          });
      }
    }
  }, [selection, tick]);
  /** Forget objects' attributes (after edits): they are fetched again while selected. */
  const invalidate = useCallback((keys: string[]) => {
    for (const k of keys) asked.current.delete(k);
    setObjects((o) => {
      const n = { ...o };
      for (const k of keys) delete n[k];
      return n;
    });
    setTick((t) => t + 1);
  }, []);
  return { objects, invalidate };
}
