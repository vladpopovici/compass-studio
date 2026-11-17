// Annotation layers of the current image: stores > layers > groups, with
// visibility, sizes, fill and outline, opacity and per-group colours.
// Sliders show only what is drawn at the current zoom.
import { Eye, EyeOff, LoaderCircle, Lock, LockOpen } from "lucide-react";
import type { LayerInfo, Schemas } from "@/api";
import { Button } from "@/components/ui/button";
import {
  ASSUMED_MPP, bandAt, BANDS, groupColor, LINE_WIDTHS, MIN_SHAPE_PX, POINT_SIZES, umPerScreenPx,
  type AnnStyle, type BandId, type Range,
} from "@/viewer/annstyle";
import { MAX_FILTER_GROUPS } from "@/viewer/points";

/** A whole-layer fetch (points, polylines). */
export type WholeLoad =
  | { state: "loading" }
  | { state: "error"; message: string }
  | { state: "ready"; count: number; groupCounts: number[] };

export interface LayerState { info: LayerInfo; style: AnnStyle; points: WholeLoad | null; lines: WholeLoad | null }

export interface Scale { mpp: number; assumed: boolean; zoom: number }

const DRAWN = new Set(["point", "polygon", "polyline"]);
const fmt = (n: number) => n.toLocaleString();

function countsText(counts: Record<string, number>) {
  return Object.entries(counts).filter(([, n]) => n > 0).map(([t, n]) => `${fmt(n)} ${t}${n === 1 ? "" : "s"}`).join(", ");
}

/** Slider step: about 1 % of the range, rounded to 1, 2 or 5 x 10^k. */
function step(r: Range) {
  const raw = (r.max - r.min) / 100, p = 10 ** Math.floor(Math.log10(raw));
  return [1, 2, 5, 10].map((m) => m * p).find((v) => v >= raw) ?? raw;
}

function px(um: number, scale: Scale) {
  const v = um / umPerScreenPx(scale.mpp, scale.zoom);
  return v < 1 ? "<1 px" : `${v.toFixed(v < 10 ? 1 : 0)} px`;
}

/** A µm slider for the current band; each band keeps its own value. */
function BandSlider({ label, ranges, values, band, scale, onChange }: {
  label: string; ranges: Record<BandId, Range>; values: Record<BandId, number>; band: BandId; scale: Scale;
  onChange: (v: Record<BandId, number>) => void;
}) {
  const r = ranges[band], v = values[band];
  const others = BANDS.map((b) => `${b.name} ${values[b.id]} µm`).join(", ");
  return (
    <label className="mt-1 flex items-center gap-2 text-muted-foreground"
           title={`${label} at ${BANDS.find((b) => b.id === band)!.name} (${r.min} to ${r.max} µm); now ${px(v, scale)} on screen. Each band of zoom keeps its own value: ${others}.`}>
      <span className="w-14 shrink-0">{label}</span>
      <input type="range" min={r.min} max={r.max} step={step(r)} value={v} className="min-w-0 flex-1 accent-primary"
             aria-label={`${label} (µm)`} onChange={(e) => onChange({ ...values, [band]: +e.target.value })} />
      <span className="w-14 shrink-0 text-right font-mono tabular-nums">{v} µm</span>
    </label>
  );
}

/** Save and discard for a layer with unsaved edits. */
export interface LayerEdit { onSave: (layerId: string) => void; onDiscard: (layerId: string) => void }

/** The working layer (its id): drawn on top, the only layer whose objects can be selected. */
export type Work = string;

/** A layer's name as shown: "(editable)" while it has edits after its save point. */
export function layerName(info: LayerInfo) {
  return info.editable && (info.unsaved ?? 0) > 0 ? `${info.name} (editable)` : info.name;
}

function LayerCard({ l, scale, onChange, edit, work, onWork }: {
  l: LayerState; scale: Scale; onChange: (s: AnnStyle) => void; edit?: LayerEdit;
  work: Work | null; onWork: (w: Work | null) => void;
}) {
  const { info, style } = l;
  const unsaved = info.editable ? info.unsaved ?? 0 : 0;
  const locked = work === info.id;
  const has = (t: string) => (info.counts[t] ?? 0) > 0;
  const drawable = Object.keys(info.counts).some((t) => DRAWN.has(t) && has(t));
  const hidden = new Set(style.hidden);
  const set = (patch: Partial<AnnStyle>) => onChange({ ...style, ...patch });
  const toggle = (i: number) => set({ hidden: hidden.has(i) ? style.hidden.filter((x) => x !== i) : [...style.hidden, i] });
  const other = Object.entries(info.counts).filter(([t, n]) => !DRAWN.has(t) && n > 0);
  const loads = [l.points, l.lines].filter((x): x is WholeLoad => !!x);
  const band = bandAt(scale.mpp, scale.zoom);
  const shapes = has("polygon");
  const sizeLabel = has("point") ? "Points" : "Centres";
  const groupCounts = loads.some((x) => x.state === "ready")
    ? info.groups.map((g) => loads.reduce((a, x) => a + (x.state === "ready" ? x.groupCounts[g.index] ?? 0 : 0), 0))
    : null;
  return (
    <section className={`border-b border-border p-2 ${unsaved ? "border-l-2 border-l-primary bg-editing" : ""} ${locked ? "ring-1 ring-primary/60 ring-inset" : ""}`}
             data-layer={info.id} data-points={info.counts.point ?? 0} data-unsaved={unsaved} data-working={locked || undefined}
             data-polygons={info.counts.polygon ?? 0} data-lines={info.counts.polyline ?? 0}>
      {unsaved > 0 && edit && (
        <div className="mb-1 flex items-center gap-1">
          <span className="flex-1 text-muted-foreground" title="Edits after the last save point; they are already stored in the curation database">
            {unsaved} unsaved edit{unsaved === 1 ? "" : "s"}
          </span>
          <Button variant="outline" className="h-6 px-2" onClick={() => edit.onDiscard(info.id)}
                  title="Revert the layer to its last save point (Redo brings the edits back)">Discard edits</Button>
          <Button className="h-6 bg-primary px-2 text-primary-foreground hover:bg-primary/90" onClick={() => edit.onSave(info.id)}
                  title="Make the edited layer the layer: one layer from now on">Save edits</Button>
        </div>
      )}
      <div className="flex items-center gap-1">
        <Button size="icon" aria-label={style.visible ? "Hide layer" : "Show layer"} disabled={!drawable}
                onClick={() => set({ visible: !style.visible })}>
          {style.visible && drawable ? <Eye /> : <EyeOff />}
        </Button>
        <span className="flex-1 truncate font-medium" title={info.id}>{layerName(info)}</span>
        <Button size="icon" aria-label={locked ? "Release working layer" : "Work in this layer"} aria-pressed={locked}
                disabled={!drawable} className={locked ? "text-primary" : "text-muted-foreground"}
                title={locked ? "Release the working layer (G)"
                  : "Work in this layer: drawn on top of the other layers, the only one you can select; the others dimmed (G locks the selected object's layer)"}
                onClick={() => onWork(locked ? null : info.id)}>
          {locked ? <Lock /> : <LockOpen />}
        </Button>
        {loads.some((x) => x.state === "loading") &&
          <LoaderCircle className="size-4 animate-spin text-muted-foreground" aria-label="Loading" />}
      </div>
      <p className="px-1 text-muted-foreground">{countsText(info.counts) || "empty"}</p>
      {loads.map((x, i) => x.state === "error" && <p key={i} className="px-1 text-red-400">{x.message}</p>)}
      {other.length > 0 && (
        <p className="px-1 text-muted-foreground italic">{other.map(([t]) => t).join(", ")}: not drawn yet</p>
      )}
      {drawable && (
        <>
          <p className="mt-1 px-1 text-muted-foreground" data-size-hint data-band={band.id}
             title="Sizes and widths are in microns; each band of zoom keeps its own values.">
            {band.name}{scale.assumed ? ` (${ASSUMED_MPP} µm/px assumed)` : ""}
          </p>
          {(has("point") || shapes) && (
            <BandSlider label={sizeLabel} ranges={POINT_SIZES} values={style.sizes} band={band.id} scale={scale}
                        onChange={(sizes) => set({ sizes })} />
          )}
          {(shapes || has("polyline")) && (
            <BandSlider label="Lines" ranges={LINE_WIDTHS} values={style.widths} band={band.id} scale={scale}
                        onChange={(widths) => set({ widths })} />
          )}
          {shapes && (
            <label className="flex items-center gap-2 text-muted-foreground"
                   title="Polygons smaller than this on screen (bounding box, by area) are drawn as centre dots; larger ones as shapes.">
              <span className="w-14 shrink-0">Shapes</span>
              <input type="range" min={MIN_SHAPE_PX.min} max={MIN_SHAPE_PX.max} step={1} value={style.minShapePx}
                     className="min-w-0 flex-1 accent-primary" aria-label="Shapes from (px)"
                     onChange={(e) => set({ minShapePx: +e.target.value })} />
              <span className="w-14 shrink-0 text-right font-mono tabular-nums">≥ {style.minShapePx} px</span>
            </label>
          )}
          {shapes && (
            <div className="flex items-center gap-2 text-muted-foreground">
              <label className="flex w-14 shrink-0 items-center gap-1" title="Draw polygon outlines">
                <input type="checkbox" checked={style.outline} className="accent-primary" aria-label="Outlines"
                       onChange={() => set({ outline: !style.outline })} />
                Outline
              </label>
              <label className="flex min-w-0 flex-1 items-center gap-2" title="Polygon fill opacity (0: outlines only)">
                Fill
                <input type="range" min={0} max={1} step={0.05} value={style.fillOpacity} className="min-w-0 flex-1 accent-primary"
                       aria-label="Fill opacity" onChange={(e) => set({ fillOpacity: +e.target.value })} />
                <span className="w-14 shrink-0 text-right font-mono tabular-nums">{Math.round(style.fillOpacity * 100)} %</span>
              </label>
            </div>
          )}
          <label className="flex items-center gap-2 text-muted-foreground">
            <span className="w-14 shrink-0">Opacity</span>
            <input type="range" min={0.05} max={1} step={0.05} value={style.opacity} className="min-w-0 flex-1 accent-primary"
                   onChange={(e) => set({ opacity: +e.target.value })} aria-label="Layer opacity" />
            <span className="w-14 shrink-0 text-right font-mono tabular-nums">{Math.round(style.opacity * 100)} %</span>
          </label>
          <div className="mt-1 flex gap-1">
            <Button variant="outline" onClick={() => set({ hidden: [] })}>All</Button>
            <Button variant="outline" onClick={() => set({ hidden: info.groups.map((g) => g.index) })}>None</Button>
          </div>
          {has("point") && info.groups.length > MAX_FILTER_GROUPS && (
            <p className="px-1 text-muted-foreground">More than {MAX_FILTER_GROUPS} groups: point group toggles are off.</p>
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
                {groupCounts && <span className="font-mono text-muted-foreground tabular-nums">{fmt(groupCounts[g.index])}</span>}
              </div>
            ))}
          </div>
        </>
      )}
    </section>
  );
}

export function LayersPanel({ stores, layers, mpp, zoom, onChange, edit, work, onWork }: {
  stores: Schemas["Store"][]; layers: LayerState[]; mpp: number | null | undefined; zoom: number;
  onChange: (id: string, s: AnnStyle) => void; edit?: LayerEdit;
  work: Work | null; onWork: (w: Work | null) => void;
}) {
  const scale: Scale = { mpp: mpp || ASSUMED_MPP, assumed: !mpp, zoom };
  if (!stores.length) return <p className="p-2 text-muted-foreground">No annotation stores for this image.</p>;
  return (
    <div>
      {stores.map((s) => (
        <div key={s.id}>
          <h3 className="border-b border-border bg-muted px-2 py-1 font-semibold" title={s.id}>{s.name}</h3>
          {layers.filter((l) => l.info.store_id === s.id).map((l) => (
            <LayerCard key={l.info.id} l={l} scale={scale} onChange={(st) => onChange(l.info.id, st)} edit={edit}
                       work={work} onWork={onWork} />
          ))}
        </div>
      ))}
    </div>
  );
}
