// Application: image picker, pan/zoom viewer, sidebar (annotation layers,
// label maps), inspector for selected points, status bar.
// URL: /?image=<id>&x=&y=&z=#token=<token>  (position kept on reload; the
// token stays in the fragment, which the browser never sends).

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { getJSON, type ImageInfo } from "@/api";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { TooltipProvider } from "@/components/ui/tooltip";
import { DEFAULT_STYLE, type LabelClass, type LabelStyle } from "@/viewer/labels";
import type { PointPick } from "@/viewer/points";
import type { View, Viewer } from "@/viewer/viewer";
import { useAnnotations, useObjects } from "./annotations";
import { Inspector, objectKey } from "./Inspector";
import { LabelPanel, type LabelMapState } from "./LabelPanel";
import { LayersPanel } from "./LayersPanel";
import { Sidebar } from "./Sidebar";
import { StatusBar } from "./StatusBar";
import { Toolbar } from "./Toolbar";
import { ViewerPane } from "./ViewerPane";

type Load = { state: "loading" } | { state: "error"; message: string } | { state: "ready"; images: ImageInfo[] };

function initialView() {
  const q = new URLSearchParams(location.search);
  const num = (k: string) => (q.has(k) && Number.isFinite(+q.get(k)!) ? +q.get(k)! : undefined);
  return { image: q.get("image"), x: num("x"), y: num("y"), zoom: num("z") };
}

function writeUrl(image: string, v: View | null) {
  const q = new URLSearchParams({ image });
  if (new URLSearchParams(location.search).has("debug")) q.set("debug", "");
  if (v) {
    q.set("x", v.x.toFixed(0));
    q.set("y", v.y.toFixed(0));
    q.set("z", v.zoom.toFixed(2));
  }
  history.replaceState(null, "", `?${q}${location.hash}`);
}

export function App() {
  const [load, setLoad] = useState<Load>({ state: "loading" });
  const [viewer, setViewer] = useState<Viewer | null>(null);
  const [imageId, setImageId] = useState<string | null>(null);
  const [view, setView] = useState<View | null>(null);
  const [cursor, setCursor] = useState<[number, number] | null>(null);
  const [tiles, setTiles] = useState("");
  const [styles, setStyles] = useState<Record<string, LabelStyle>>({});
  const [values, setValues] = useState<Record<string, number[]>>({});
  const [left, setLeft] = useState(true);
  const [right, setRight] = useState(false);
  const [tab, setTab] = useState<string | null>(null);
  const [selection, setSelection] = useState<PointPick[]>([]);
  const [hoverPick, setHoverPick] = useState<PointPick | null>(null);
  const restore = useRef(initialView());

  useEffect(() => {
    getJSON<ImageInfo[]>("images").then(
      (images) => {
        setLoad({ state: "ready", images });
        const want = restore.current.image;
        const slides = images.filter((i) => i.role !== "labels");
        setImageId(slides.find((i) => i.id === want)?.id ?? slides[0]?.id ?? null);
      },
      (e: Error) => setLoad({
        state: "error",
        message: e.message.endsWith("401") ? "Session token missing or wrong: open the URL the server printed." : e.message,
      }));
  }, []);

  const all = useMemo(() => (load.state === "ready" ? load.images : []), [load]);
  const images = useMemo(() => all.filter((i) => i.role !== "labels"), [all]);
  const image = images.find((i) => i.id === imageId) ?? null;
  const labelInfos = useMemo(() => all.filter((i) => i.role === "labels" && i.aligned_to === imageId), [all, imageId]);
  const maps: LabelMapState[] = labelInfos.map((info) => ({
    info, style: styles[info.id] ?? DEFAULT_STYLE, values: values[info.id] ?? [],
  }));

  useEffect(() => {
    if (!viewer) return;
    const r = restore.current;
    const keep = image && r.image === image.id;
    viewer.setImage(image, keep ? { x: r.x, y: r.y, zoom: r.zoom } : undefined);
  }, [viewer, image]);

  useEffect(() => {
    if (!viewer) return;
    viewer.setLabels(labelInfos.map((info) => ({ info, style: styles[info.id] ?? DEFAULT_STYLE })));
    // maps without a class table: list the values found on the coarsest level
    for (const info of labelInfos) {
      if (info.classes?.length || values[info.id]) continue;
      viewer.labelSource(info.id)?.discover().then(
        (v) => setValues((old) => ({ ...old, [info.id]: v })),
        () => setValues((old) => ({ ...old, [info.id]: [] })));
    }
  }, [viewer, labelInfos, styles, values]);

  const setStyle = useCallback((id: string, s: LabelStyle) => setStyles((old) => ({ ...old, [id]: s })), []);

  const ann = useAnnotations(image?.id ?? null);
  // ?debug exposes the viewer for end-to-end measurements
  useEffect(() => {
    if (viewer && new URLSearchParams(location.search).has("debug"))
      (window as unknown as { __cs?: unknown }).__cs = { viewer, ann };
  });
  const objects = useObjects(selection);
  const layerById = useMemo(() => new Map(ann.layers.map((l) => [l.info.id, l.info])), [ann.layers]);
  useEffect(() => {
    viewer?.setPoints(ann.drawn);
  }, [viewer, ann.drawn]);
  useEffect(() => {
    viewer?.setSelection(selection);
  }, [viewer, selection]);
  useEffect(() => {
    setSelection([]);
    setHoverPick(null);
  }, [imageId]);

  const onHover = useCallback((p: [number, number] | null, pickInfo: PointPick | null) => {
    setCursor(p);
    setHoverPick(pickInfo);
  }, []);
  const onClick = useCallback((pk: PointPick | null, add: boolean) => {
    if (!pk) {
      if (!add) setSelection([]);
      return;
    }
    setSelection((old) => {
      const k = objectKey(pk);
      if (!add) return [pk];
      return old.some((o) => objectKey(o) === k) ? old.filter((o) => objectKey(o) !== k) : [...old, pk];
    });
    setRight(true);
  }, []);

  const hoverPoint = useMemo(() => {
    if (!hoverPick) return "";
    const l = layerById.get(hoverPick.layerId);
    const g = l?.groups.find((x) => x.index === hoverPick.group);
    return `${l?.name ?? hoverPick.layerId} › ${g?.name ?? hoverPick.group} #${hoverPick.objectId}`;
  }, [hoverPick, layerById]);

  const hoverLabels = useMemo(() => {
    if (!viewer || !cursor) return "";
    return viewer.labelsAt(cursor[0], cursor[1]).map(({ info, value }) => {
      if (value === info.background) return "";
      const c = ((info.classes ?? []) as LabelClass[]).find((x) => x.value === value);
      return `${info.name ?? info.id}: ${value}${c ? ` ${c.name}` : ""}`;
    }).filter(Boolean).join("  ");
    // styles: visibility changes what is under the cursor
  }, [viewer, cursor, styles]); // eslint-disable-line react-hooks/exhaustive-deps

  // URL follows the view, at most every 300 ms
  useEffect(() => {
    if (!image) return;
    const t = setTimeout(() => writeUrl(image.id, view), 300);
    return () => clearTimeout(t);
  }, [image, view]);

  useEffect(() => {
    if (!viewer) return;
    const t = setInterval(() => setTiles(viewer.stats.summaryLine()), 500);
    return () => clearInterval(t);
  }, [viewer]);

  // a picked image opens fitted; only the image in the initial URL restores its view
  const pick = useCallback((id: string) => {
    restore.current.image = null;
    setImageId(id);
  }, []);

  useEffect(() => {
    if (!viewer) return;
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (e.ctrlKey || e.metaKey || e.altKey || t?.closest("input, textarea, select, [role=listbox], [contenteditable]"))
        return;
      const step = e.shiftKey ? 0.5 : 0.2;
      const actions: Record<string, () => void> = {
        ArrowLeft: () => viewer.panBy(-step, 0), ArrowRight: () => viewer.panBy(step, 0),
        ArrowUp: () => viewer.panBy(0, -step), ArrowDown: () => viewer.panBy(0, step),
        "+": () => viewer.zoomBy(1), "=": () => viewer.zoomBy(1), "-": () => viewer.zoomBy(-1),
        "0": () => viewer.fit(), f: () => viewer.fit(), "1": () => viewer.zoomTo(0),
        PageDown: () => cycle(1), PageUp: () => cycle(-1),
        Escape: () => setSelection([]),
        "[": () => setLeft((v) => !v), "]": () => setRight((v) => !v),
        l: () => setStyles((old) => {
          const anyVisible = labelInfos.some((i) => (old[i.id] ?? DEFAULT_STYLE).visible);
          return Object.fromEntries(labelInfos.map((i) => [i.id, { ...(old[i.id] ?? DEFAULT_STYLE), visible: !anyVisible }]));
        }),
        o: () => setStyles((old) => {
          const anyOutline = labelInfos.some((i) => (old[i.id] ?? DEFAULT_STYLE).outline);
          return Object.fromEntries(labelInfos.map((i) => [i.id, { ...(old[i.id] ?? DEFAULT_STYLE), outline: !anyOutline }]));
        }),
      };
      const cycle = (d: number) => {
        if (!images.length) return;
        const i = images.findIndex((x) => x.id === imageId);
        pick(images[(i + d + images.length) % images.length].id);
      };
      const a = actions[e.key];
      if (a) {
        e.preventDefault();
        a();
      }
    };
    addEventListener("keydown", onKey);
    return () => removeEventListener("keydown", onKey);
  }, [viewer, images, imageId, pick, labelInfos]);

  return (
    <TooltipProvider>
      <div className="flex h-full flex-col">
        <Toolbar images={images} image={image} view={view} onImage={pick}
                 onZoomBy={(d) => viewer?.zoomBy(d)} onFit={() => viewer?.fit()} onOneToOne={() => viewer?.zoomTo(0)}
                 left={left} onLeft={() => setLeft((v) => !v)} right={right} onRight={() => setRight((v) => !v)} />
        <div className="flex min-h-0 flex-1">
          {left && (
            <Sidebar side="left" label="Sidebar">
              <Tabs value={tab ?? (ann.layers.length || !maps.length ? "layers" : "labels")} onValueChange={setTab}
                    className="flex min-h-0 flex-1 flex-col">
                <TabsList>
                  <TabsTrigger value="layers">Layers <span className="text-muted-foreground">{ann.layers.length}</span></TabsTrigger>
                  <TabsTrigger value="labels">Labels <span className="text-muted-foreground">{maps.length}</span></TabsTrigger>
                </TabsList>
                <TabsContent value="layers">
                  {ann.error && <p className="p-2 text-red-400">{ann.error}</p>}
                  <LayersPanel stores={ann.stores} layers={ann.layers} mpp={image?.mpp} zoom={view?.zoom ?? 0}
                               onChange={ann.setStyle} />
                </TabsContent>
                <TabsContent value="labels"><LabelPanel maps={maps} onChange={setStyle} /></TabsContent>
              </Tabs>
            </Sidebar>
          )}
          <main className="relative min-w-0 flex-1">
            <ViewerPane onReady={setViewer} onView={setView} onHover={onHover} onClick={onClick} />
            {load.state !== "ready" || !image ? (
              <div className="pointer-events-none absolute inset-0 flex items-center justify-center text-sm text-muted-foreground">
                {load.state === "loading" ? "Loading…" : load.state === "error" ? load.message : "No images served."}
              </div>
            ) : null}
          </main>
          {right && (
            <Sidebar side="right" label="Inspector">
              <Inspector selection={selection} layers={layerById} objects={objects} mpp={image?.mpp}
                         onClear={() => setSelection([])} onClose={() => setRight(false)}
                         onRemove={(p) => setSelection((old) => old.filter((o) => objectKey(o) !== objectKey(p)))} />
            </Sidebar>
          )}
        </div>
        <StatusBar image={image} view={view} cursor={cursor} tiles={tiles} labels={hoverLabels} point={hoverPoint} />
      </div>
    </TooltipProvider>
  );
}
