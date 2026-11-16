// Selected objects: identity, group, position and their sparse attributes
// (fetched lazily by id, GET /layers/{id}/objects).
import { X } from "lucide-react";
import type { LayerInfo, Schemas } from "@/api";
import { Button } from "@/components/ui/button";
import type { ObjectPick } from "@/viewer/annstyle";

export type ObjectInfo = Schemas["ObjectInfo"];

const fmt = (n: number, d = 0) => n.toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });

function value(v: number) {
  return Number.isInteger(v) ? fmt(v) : Math.abs(v) >= 1e4 || Math.abs(v) < 1e-3 ? v.toExponential(3) : v.toPrecision(4);
}

/** Area or length: 4 significant digits, grouped thousands, no exponent. */
function measure(v: number) {
  return v >= 1000 ? fmt(Math.round(v)) : v.toPrecision(4).replace(/\.?0+$/, "");
}

export function objectKey(p: { layerId: string; objectId: number }) {
  return `${p.layerId}|${p.objectId}`;
}

export function Inspector({ selection, layers, objects, mpp, onClear, onRemove, onClose }: {
  selection: ObjectPick[]; layers: Map<string, LayerInfo>; objects: Record<string, ObjectInfo | "error">;
  mpp: number | null | undefined; onClear: () => void; onRemove: (p: ObjectPick) => void; onClose: () => void;
}) {
  return (
    <>
      <div className="flex h-8 shrink-0 items-center gap-1 border-b border-border px-2">
        <h2 className="flex-1 font-semibold">Inspector</h2>
        {selection.length > 1 && <span className="text-muted-foreground">{selection.length} selected</span>}
        {selection.length > 0 && <Button variant="outline" onClick={onClear} title="Clear selection (Esc)">Clear</Button>}
        <Button size="icon" aria-label="Close inspector" title="Close (])" onClick={onClose}><X /></Button>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {!selection.length && (
          <p className="p-2 text-muted-foreground">Click an object to inspect it; Shift+click adds to the selection.</p>
        )}
        {selection.map((p) => {
          const layer = layers.get(p.layerId);
          const o = objects[objectKey(p)];
          const group = layer?.groups.find((g) => g.index === p.group);
          const values = o && o !== "error" ? Object.entries(o.values).sort(([a], [b]) => a.localeCompare(b)) : [];
          return (
            <section key={objectKey(p)} className="border-b border-border p-2" data-object={p.objectId}>
              <div className="flex items-center gap-1">
                <span className="flex-1 truncate font-medium">
                  {o && o !== "error" && o.name ? o.name : `#${p.objectId}`}
                </span>
                <Button size="icon" aria-label="Remove from selection" onClick={() => onRemove(p)}><X /></Button>
              </div>
              <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 px-1">
                <dt className="text-muted-foreground">Id</dt><dd className="font-mono tabular-nums">{p.objectId}</dd>
                <dt className="text-muted-foreground">Layer</dt><dd className="truncate">{layer?.name ?? p.layerId}</dd>
                <dt className="text-muted-foreground">Group</dt><dd className="truncate">{group?.name ?? p.group}</dd>
                <dt className="text-muted-foreground">Type</dt><dd>{o && o !== "error" ? o.type : p.kind}</dd>
                <dt className="text-muted-foreground">{p.kind === "point" ? "Position" : "Centre"}</dt>
                <dd className="font-mono tabular-nums">{fmt(p.x, 1)}, {fmt(p.y, 1)} px</dd>
                {mpp && <><dt /><dd className="font-mono tabular-nums">{fmt(p.x * mpp, 1)}, {fmt(p.y * mpp, 1)} µm</dd></>}
                {o && o !== "error" && o.area != null && <>
                  <dt className="text-muted-foreground">Area</dt>
                  <dd className="font-mono tabular-nums" data-measure="area">{mpp ? `${measure(o.area * mpp * mpp)} µm²` : `${measure(o.area)} px²`}</dd>
                </>}
                {o && o !== "error" && o.length != null && <>
                  <dt className="text-muted-foreground">{p.kind === "polygon" ? "Perimeter" : "Length"}</dt>
                  <dd className="font-mono tabular-nums" data-measure="length">{mpp ? `${measure(o.length * mpp)} µm` : `${measure(o.length)} px`}</dd>
                </>}
              </dl>
              {o === undefined && <p className="mt-1 px-1 text-muted-foreground">Loading attributes…</p>}
              {o === "error" && <p className="mt-1 px-1 text-red-400">Attributes could not be loaded.</p>}
              {o && o !== "error" && (
                values.length ? (
                  <table className="mt-1 w-full">
                    <tbody>
                      {values.map(([k, v]) => (
                        <tr key={k} className="hover:bg-accent">
                          <td className="truncate px-1 text-muted-foreground">{k}</td>
                          <td className="px-1 text-right font-mono tabular-nums">{value(v)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                ) : <p className="mt-1 px-1 text-muted-foreground">No attributes.</p>
              )}
            </section>
          );
        })}
      </div>
    </>
  );
}
