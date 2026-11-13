// Annotation layers of the current image: stores > layers > groups, with
// visibility, point size, opacity and per-group colours.
import { Eye, EyeOff, LoaderCircle } from "lucide-react";
import type { LayerInfo, Schemas } from "@/api";
import { Button } from "@/components/ui/button";
import { groupColor, MAX_FILTER_GROUPS, type PointStyle } from "@/viewer/points";

export type PointLoad =
  | { state: "loading" }
  | { state: "error"; message: string }
  | { state: "ready"; count: number; groupCounts: number[] };

export interface LayerState { info: LayerInfo; style: PointStyle; load: PointLoad | null }

const fmt = (n: number) => n.toLocaleString();

function countsText(counts: Record<string, number>) {
  return Object.entries(counts).filter(([, n]) => n > 0).map(([t, n]) => `${fmt(n)} ${t}${n === 1 ? "" : "s"}`).join(", ");
}

function LayerCard({ l, onChange }: { l: LayerState; onChange: (s: PointStyle) => void }) {
  const { info, style, load } = l;
  const points = (info.counts.point ?? 0) > 0;
  const hidden = new Set(style.hidden);
  const set = (patch: Partial<PointStyle>) => onChange({ ...style, ...patch });
  const toggle = (i: number) => set({ hidden: hidden.has(i) ? style.hidden.filter((x) => x !== i) : [...style.hidden, i] });
  const other = Object.entries(info.counts).filter(([t, n]) => t !== "point" && n > 0);
  const ready = load?.state === "ready" ? load : null;
  return (
    <section className="border-b border-border p-2" data-layer={info.id} data-points={info.counts.point ?? 0}>
      <div className="flex items-center gap-1">
        <Button size="icon" aria-label={style.visible ? "Hide layer" : "Show layer"} disabled={!points}
                onClick={() => set({ visible: !style.visible })}>
          {style.visible && points ? <Eye /> : <EyeOff />}
        </Button>
        <span className="flex-1 truncate font-medium" title={info.id}>{info.name}</span>
        {load?.state === "loading" && <LoaderCircle className="size-4 animate-spin text-muted-foreground" aria-label="Loading" />}
      </div>
      <p className="px-1 text-muted-foreground">{countsText(info.counts) || "empty"}</p>
      {load?.state === "error" && <p className="px-1 text-red-400">{load.message}</p>}
      {other.length > 0 && (
        <p className="px-1 text-muted-foreground italic">
          {other.map(([t]) => t).join(", ")}: not drawn yet{points ? "" : " (points only in this version)"}
        </p>
      )}
      {points && (
        <>
          <label className="mt-1 flex items-center gap-2 text-muted-foreground">
            Size
            <input type="range" min={1} max={12} step={0.5} value={style.size} className="flex-1 accent-primary"
                   onChange={(e) => set({ size: +e.target.value })} aria-label="Point size" />
            <span className="w-8 text-right font-mono tabular-nums">{style.size}</span>
          </label>
          <label className="flex items-center gap-2 text-muted-foreground">
            Opacity
            <input type="range" min={0.05} max={1} step={0.05} value={style.opacity} className="flex-1 accent-primary"
                   onChange={(e) => set({ opacity: +e.target.value })} aria-label="Point opacity" />
            <span className="w-8 text-right font-mono tabular-nums">{Math.round(style.opacity * 100)}</span>
          </label>
          <div className="mt-1 flex gap-1">
            <Button variant="outline" onClick={() => set({ hidden: [] })}>All</Button>
            <Button variant="outline" onClick={() => set({ hidden: info.groups.map((g) => g.index) })}>None</Button>
          </div>
          {info.groups.length > MAX_FILTER_GROUPS && (
            <p className="px-1 text-muted-foreground">More than {MAX_FILTER_GROUPS} groups: group toggles are off.</p>
          )}
          <div className="mt-1 max-h-[40vh] overflow-y-auto">
            {info.groups.map((g) => (
              <div key={g.index} className="flex items-center gap-2 rounded px-1 py-0.5 hover:bg-accent">
                <input type="checkbox" checked={!hidden.has(g.index)} onChange={() => toggle(g.index)}
                       className="accent-primary" aria-label={`Show group ${g.name}`} />
                <input type="color" value={groupColor(style, g.index)} aria-label={`Colour of ${g.name}`}
                       onChange={(e) => set({ colors: { ...style.colors, [g.index]: e.target.value } })}
                       className="size-4 shrink-0 cursor-pointer rounded-sm border border-border bg-transparent p-0"
                       style={{ opacity: hidden.has(g.index) ? 0.3 : 1 }} />
                <span className="flex-1 truncate" onClick={() => toggle(g.index)}>{g.name}</span>
                {ready && <span className="font-mono text-muted-foreground tabular-nums">{fmt(ready.groupCounts[g.index] ?? 0)}</span>}
              </div>
            ))}
          </div>
        </>
      )}
    </section>
  );
}

export function LayersPanel({ stores, layers, onChange }: {
  stores: Schemas["Store"][]; layers: LayerState[]; onChange: (id: string, s: PointStyle) => void;
}) {
  if (!stores.length) return <p className="p-2 text-muted-foreground">No annotation stores for this image.</p>;
  return (
    <div>
      {stores.map((s) => (
        <div key={s.id}>
          <h3 className="border-b border-border bg-muted px-2 py-1 font-semibold" title={s.id}>{s.name}</h3>
          {layers.filter((l) => l.info.store_id === s.id).map((l) => (
            <LayerCard key={l.info.id} l={l} onChange={(st) => onChange(l.info.id, st)} />
          ))}
        </div>
      ))}
    </div>
  );
}
