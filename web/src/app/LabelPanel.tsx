// Label maps of the current image: visibility, opacity, outline, classes.
import { memo } from "react";
import { Eye, EyeOff, Square, SquareDashed } from "lucide-react";
import type { ImageInfo } from "@/api";
import { Button } from "@/components/ui/button";
import { classColor, type LabelClass, type LabelStyle } from "@/viewer/labels";

export interface LabelMapState { info: ImageInfo; style: LabelStyle; values: number[] }

function Row({ color, name, value, shown, onToggle }: {
  color: string; name: string; value?: number; shown: boolean; onToggle: () => void;
}) {
  return (
    <label className="flex cursor-pointer items-center gap-2 rounded px-1 py-0.5 hover:bg-accent">
      <input type="checkbox" checked={shown} onChange={onToggle} className="accent-primary" />
      <span className="size-3 shrink-0 rounded-sm border border-border" style={{ background: color, opacity: shown ? 1 : 0.3 }} />
      <span className="flex-1 truncate">{name}</span>
      {value !== undefined && <span className="font-mono text-muted-foreground tabular-nums">{value}</span>}
    </label>
  );
}

function MapCard({ m, onChange }: { m: LabelMapState; onChange: (s: LabelStyle) => void }) {
  const { info, style } = m;
  const classes = (info.classes ?? []) as LabelClass[];
  const listed = new Set(classes.map((c) => c.value));
  // with no table: the values found on the coarsest level, coloured by value
  const extra = m.values.filter((v) => !listed.has(v));
  const rows = [...classes.map((c) => ({ value: c.value, name: c.name })),
                ...(classes.length ? [] : extra.map((v) => ({ value: v, name: `value ${v}` })))];
  const hidden = new Set(style.hidden);
  const set = (patch: Partial<LabelStyle>) => onChange({ ...style, ...patch });
  const toggle = (v: number) => set({ hidden: hidden.has(v) ? style.hidden.filter((x) => x !== v) : [...style.hidden, v] });
  return (
    <section className="border-b border-border p-2">
      <div className="flex items-center gap-1">
        <Button size="icon" aria-label={style.visible ? "Hide map" : "Show map"} onClick={() => set({ visible: !style.visible })}>
          {style.visible ? <Eye /> : <EyeOff />}
        </Button>
        <span className="flex-1 truncate font-medium" title={info.id}>{info.name ?? info.id}</span>
        <Button size="icon" aria-label={style.outline ? "Filled" : "Outlines"} title={style.outline ? "Outlines (o)" : "Filled (o)"}
                onClick={() => set({ outline: !style.outline })}>
          {style.outline ? <SquareDashed /> : <Square />}
        </Button>
      </div>
      <label className="mt-1 flex items-center gap-2 text-muted-foreground">
        Opacity
        <input type="range" min={0} max={1} step={0.05} value={style.opacity} className="flex-1 accent-primary"
               onChange={(e) => set({ opacity: +e.target.value })} />
        <span className="w-8 text-right font-mono tabular-nums">{Math.round(style.opacity * 100)}</span>
      </label>
      <div className="mt-1 flex gap-1">
        <Button variant="outline" onClick={() => set({ hidden: [], others: true })}>All</Button>
        <Button variant="outline" onClick={() => set({ hidden: rows.map((r) => r.value), others: false })}>None</Button>
      </div>
      <div className="mt-1 max-h-[40vh] overflow-y-auto">
        {rows.map((r) => (
          <Row key={r.value} color={classColor(classes, r.value)} name={r.name} value={r.value}
               shown={!hidden.has(r.value) && (classes.length > 0 || style.others)} onToggle={() => {
                 if (!classes.length && !style.others) set({ others: true, hidden: rows.map((x) => x.value).filter((v) => v !== r.value) });
                 else toggle(r.value);
               }} />
        ))}
        {classes.length > 0 && (
          <Row color="repeating-linear-gradient(45deg,#888 0 2px,transparent 2px 4px)" name="Other values"
               shown={style.others} onToggle={() => set({ others: !style.others })} />
        )}
        {!rows.length && <p className="px-1 text-muted-foreground">No labels found at the coarsest level.</p>}
      </div>
    </section>
  );
}

export const LabelPanel = memo(function LabelPanel({ maps, onChange }: { maps: LabelMapState[]; onChange: (id: string, s: LabelStyle) => void }) {
  if (!maps.length) return <p className="p-2 text-muted-foreground">No label maps for this image.</p>;
  return <div>{maps.map((m) => <MapCard key={m.info.id} m={m} onChange={(s) => onChange(m.info.id, s)} />)}</div>;
});
