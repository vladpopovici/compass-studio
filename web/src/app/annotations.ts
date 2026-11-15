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

  useEffect(() => {
    setStores([]);
    setLayers([]);
    setLoads({});
    setError(null);
    sources.current = new Map();
    if (!imageId) return;
    const ac = new AbortController();
    const made = sources.current;
    (async () => {
      const mine = (await getJSON<Store[]>("stores")).filter((s) => s.aligned_to === imageId);
      if (ac.signal.aborted) return;
      setStores(mine);
      const ls = (await Promise.all(mine.map((s) => getJSON<LayerInfo[]>(`layers?store=${encodeURIComponent(s.id)}`)))).flat();
      if (ac.signal.aborted) return;
      for (const l of ls) {
        const s: Sources = {};
        if (l.counts.polygon) s.polygons = new PolygonSource(l);
        if (l.counts.point) s.points = new PointSource(l);
        if (l.counts.polyline) s.lines = new LineSource(l);
        made.set(l.id, s);
      }
      setLayers(ls);
      for (const l of ls) {
        const s = made.get(l.id)!;
        for (const [kind, src] of [["points", s.points], ["lines", s.lines]] as const) {
          if (!src) continue;
          const key = `${l.id}|${kind}`;
          setLoads((o) => ({ ...o, [key]: { state: "loading" } }));
          src.load(ac.signal).then(
            () => setLoads((o) => ({ ...o, [key]: { state: "ready", count: src.count, groupCounts: src.groupCounts() } })),
            (e: Error) => {
              if (!ac.signal.aborted) setLoads((o) => ({ ...o, [key]: { state: "error", message: e.message } }));
            });
        }
      }
    })().catch((e: Error) => {
      if (!ac.signal.aborted) setError(e.message);
    });
    return () => {
      ac.abort();
      for (const s of made.values()) s.polygons?.destroy();
    };
  }, [imageId]);

  const setStyle = useCallback((id: string, s: AnnStyle) => setStyles((o) => ({ ...o, [id]: s })), []);

  const layerStates: LayerState[] = useMemo(() => layers.map((info) => ({
    info, style: styles[info.id] ?? DEFAULT_ANN_STYLE,
    points: loads[`${info.id}|points`] ?? null, lines: loads[`${info.id}|lines`] ?? null,
  })), [layers, styles, loads]);
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
  return { stores, layers: layerStates, drawn, setStyle, setStyles, error, sources };
}

/** Attributes of the selected objects, fetched by id in batches of up to 1000 per layer. */
export function useObjects(selection: ObjectPick[]) {
  const [objects, setObjects] = useState<Record<string, ObjectInfo | "error">>({});
  const asked = useRef(new Set<string>());
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
  }, [selection]);
  return objects;
}
