import { useEffect, useMemo, useState } from "react";
import type { ImageInfo } from "@/api";
import type { ObjectPick } from "@/viewer/annstyle";
import type { LabelClass } from "@/viewer/labels";
import type { View, Viewer } from "@/viewer/viewer";
import { scaleBar, shownLevel } from "./format";
import { useLive, type Live } from "./live";

const fmt = (n: number, d = 0) => n.toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });

type LayerNames = Map<string, { name: string; groups: { index: number; name: string }[] }>;

/** The status bar subscribes to the view and the cursor itself, so they never re-render the application. */
export function StatusBar({ viewer, image, view: viewLive, cursor: cursorLive, hover, layers, drawn }: {
  viewer: Viewer | null; image: ImageInfo | null; view: Live<View | null>; cursor: Live<[number, number] | null>;
  hover: Live<ObjectPick | null>; layers: LayerNames;
  /** changes whenever what is drawn under the cursor may change (label styles, channel settings) */
  drawn: unknown;
}) {
  const view = useLive(viewLive), cursor = useLive(cursorLive), pick = useLive(hover);
  const [tiles, setTiles] = useState("");
  useEffect(() => {
    if (!viewer) return;
    const t = setInterval(() => setTiles(viewer.stats.summaryLine()), 500);
    return () => clearInterval(t);
  }, [viewer]);

  const point = useMemo(() => {
    if (!pick) return "";
    const l = layers.get(pick.layerId);
    const g = l?.groups.find((x) => x.index === pick.group);
    return `${l?.name ?? pick.layerId} › ${g?.name ?? pick.group} #${pick.objectId}`;
  }, [pick, layers]);

  const labels = useMemo(() => {
    if (!viewer || !cursor) return "";
    const channels = image ? viewer.channelsAt(cursor[0], cursor[1]).map(({ channel, value }) => `${image.channels[channel]} ${value}`) : [];
    return [...channels, ...viewer.labelsAt(cursor[0], cursor[1]).map(({ info, value }) => {
      if (value === info.background) return "";
      const c = ((info.classes ?? []) as LabelClass[]).find((x) => x.value === value);
      return `${info.name ?? info.id}: ${value}${c ? ` ${c.name}` : ""}`;
    })].filter(Boolean).join("  ");
  }, [viewer, cursor, drawn, image]); // eslint-disable-line react-hooks/exhaustive-deps

  const inside = image && cursor && cursor[0] >= 0 && cursor[1] >= 0 && cursor[0] < image.width && cursor[1] < image.height;
  const bar = image && view ? scaleBar(image.mpp, view.zoom) : null;
  return (
    <footer className="flex h-6 shrink-0 items-center gap-4 border-t border-border px-2 font-mono tabular-nums text-muted-foreground">
      <span className="w-40" title="Cursor, level-0 px">
        {inside ? `${fmt(cursor[0])}, ${fmt(cursor[1])} px` : "–"}
      </span>
      <span className="w-44" title="Cursor, microns">
        {inside && image.mpp ? `${fmt(cursor[0] * image.mpp, 1)}, ${fmt(cursor[1] * image.mpp, 1)} µm` : ""}
      </span>
      {image && view && (
        <span title="Pyramid level shown">L{shownLevel(view.zoom, image.levels.length)}/{image.levels.length - 1}</span>
      )}
      {image?.mpp && <span title="Microns per level-0 px">{image.mpp.toPrecision(3)} µm/px</span>}
      <span className="truncate text-foreground" title="Annotation object under the cursor">{point}</span>
      <span className="truncate text-foreground" title="Label under the cursor">{labels}</span>
      <span className="flex-1 truncate text-right" title="Tile loading">{tiles}</span>
      {bar && (
        <span className="flex items-center gap-2" title="Scale">
          <span className="text-foreground">{bar.label}</span>
          <span className="h-1.5 border-x border-b border-foreground" style={{ width: bar.px }} />
        </span>
      )}
    </footer>
  );
}
