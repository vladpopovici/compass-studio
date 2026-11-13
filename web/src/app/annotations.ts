// Annotation stores and point layers of the current image, and the
// attributes of selected objects (fetched lazily, cached per session).
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { getJSON, type LayerInfo, type Schemas } from "@/api";
import { DEFAULT_POINT_STYLE, PointSource, type PointPick, type PointStyle } from "@/viewer/points";
import { objectKey, type ObjectInfo } from "./Inspector";
import type { LayerState, PointLoad } from "./LayersPanel";

type Store = Schemas["Store"];

export function useAnnotations(imageId: string | null) {
  const [stores, setStores] = useState<Store[]>([]);
  const [layers, setLayers] = useState<LayerInfo[]>([]);
  const [loads, setLoads] = useState<Record<string, PointLoad>>({});
  const [styles, setStyles] = useState<Record<string, PointStyle>>({});
  const [error, setError] = useState<string | null>(null);
  const sources = useRef(new Map<string, PointSource>());

  useEffect(() => {
    setStores([]);
    setLayers([]);
    setLoads({});
    setError(null);
    sources.current = new Map();
    if (!imageId) return;
    const ac = new AbortController();
    (async () => {
      const mine = (await getJSON<Store[]>("stores")).filter((s) => s.aligned_to === imageId);
      if (ac.signal.aborted) return;
      setStores(mine);
      const ls = (await Promise.all(mine.map((s) => getJSON<LayerInfo[]>(`layers?store=${encodeURIComponent(s.id)}`)))).flat();
      if (ac.signal.aborted) return;
      setLayers(ls);
      for (const l of ls) {
        if (!(l.counts.point ?? 0)) continue;
        const src = new PointSource(l);
        sources.current.set(l.id, src);
        setLoads((o) => ({ ...o, [l.id]: { state: "loading" } }));
        src.load(ac.signal).then(
          () => setLoads((o) => ({ ...o, [l.id]: { state: "ready", count: src.count, groupCounts: src.groupCounts() } })),
          (e: Error) => {
            if (!ac.signal.aborted) setLoads((o) => ({ ...o, [l.id]: { state: "error", message: e.message } }));
          });
      }
    })().catch((e: Error) => {
      if (!ac.signal.aborted) setError(e.message);
    });
    return () => ac.abort();
  }, [imageId]);

  const setStyle = useCallback((id: string, s: PointStyle) => setStyles((o) => ({ ...o, [id]: s })), []);

  const layerStates: LayerState[] = useMemo(() => layers.map((info) => ({
    info, style: styles[info.id] ?? DEFAULT_POINT_STYLE, load: loads[info.id] ?? null,
  })), [layers, styles, loads]);
  /** loaded point layers with their styles, bottom first */
  const drawn = useMemo(() => layerStates.flatMap((l) => {
    const source = sources.current.get(l.info.id);
    return source && l.load?.state === "ready" ? [{ source, style: l.style }] : [];
  }), [layerStates]);
  return { stores, layers: layerStates, drawn, setStyle, setStyles, error };
}

/** Attributes of the selected objects, fetched by id in batches of up to 1000 per layer. */
export function useObjects(selection: PointPick[]) {
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
