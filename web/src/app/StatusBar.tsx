import type { ImageInfo } from "@/api";
import type { View } from "@/viewer/viewer";
import { scaleBar, shownLevel } from "./format";

const fmt = (n: number, d = 0) => n.toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });

export function StatusBar({ image, view, cursor, tiles, labels, point }: {
  image: ImageInfo | null; view: View | null; cursor: [number, number] | null; tiles: string; labels: string;
  point: string;
}) {
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
