// Selected objects: identity, group, position and their sparse attributes
// (fetched lazily by id, GET /layers/{id}/objects). With an editing server,
// the group and attributes are editable (milestone 7): each change is one
// commit; the block at the top applies to every selected object.
import { Plus, X } from "lucide-react";
import { useState, memo } from "react";
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

export interface InspectorEdit {
  onGroup: (picks: ObjectPick[], group: number) => void;
  onValues: (picks: ObjectPick[], values: Record<string, number | null>) => void;
  onDelete: () => void;
  /** attribute names known to the stores, for suggestions */
  names: string[];
}

/** A number field that commits on Enter or when it loses focus (empty: remove). */
function ValueField({ value, label, onCommit }: { value: number; label: string; onCommit: (v: number | null) => void }) {
  const [text, setText] = useState<string | null>(null);
  const commit = () => {
    if (text === null) return;
    const t = text.trim();
    setText(null);
    if (t === "") onCommit(null);
    else if (Number.isFinite(+t) && +t !== value) onCommit(+t);
  };
  return (
    <input value={text ?? String(value)} aria-label={label} inputMode="decimal"
           className="w-full rounded border border-transparent bg-transparent px-1 text-right font-mono tabular-nums hover:border-border focus:border-border"
           onChange={(e) => setText(e.target.value)} onBlur={commit}
           onKeyDown={(e) => {
             if (e.key === "Enter") (e.target as HTMLInputElement).blur();
             if (e.key === "Escape") { setText(null); (e.target as HTMLInputElement).blur(); }
           }} />
  );
}

/** Name and value of an attribute to set; suggestions from the stores. */
function AddValue({ names, onAdd, label }: { names: string[]; onAdd: (name: string, v: number) => void; label: string }) {
  const [name, setName] = useState("");
  const [value, setValue] = useState("");
  const ok = name.trim() !== "" && value.trim() !== "" && Number.isFinite(+value);
  return (
    <form className="mt-1 flex items-center gap-1 px-1" onSubmit={(e) => {
      e.preventDefault();
      if (!ok) return;
      onAdd(name.trim(), +value);
      setName("");
      setValue("");
    }}>
      <input value={name} onChange={(e) => setName(e.target.value)} placeholder="attribute" list="cs-attribute-names"
             aria-label={`${label} name`} className="min-w-0 flex-1 rounded border border-border bg-transparent px-1" />
      <input value={value} onChange={(e) => setValue(e.target.value)} placeholder="value" inputMode="decimal"
             aria-label={`${label} value`} className="w-16 rounded border border-border bg-transparent px-1 text-right font-mono" />
      <Button size="icon" type="submit" disabled={!ok} aria-label={label} title={label}><Plus /></Button>
    </form>
  );
}

function GroupSelect({ layer, value, onChange, label }: {
  layer: LayerInfo; value: number | null; onChange: (g: number) => void; label: string;
}) {
  return (
    <select value={value ?? ""} aria-label={label} onChange={(e) => onChange(+e.target.value)}
            className="w-full truncate rounded border border-transparent bg-transparent hover:border-border">
      {value === null && <option value="">mixed</option>}
      {layer.groups.map((g) => <option key={g.index} value={g.index} className="bg-background">{g.name}</option>)}
    </select>
  );
}

export function objectKey(p: { layerId: string; objectId: number }) {
  return `${p.layerId}|${p.objectId}`;
}

export const Inspector = memo(function Inspector({ selection, layers, objects, mpp, onClear, onRemove, onClose, edit }: {
  selection: ObjectPick[]; layers: Map<string, LayerInfo>; objects: Record<string, ObjectInfo | "error">;
  mpp: number | null | undefined; onClear: () => void; onRemove: (p: ObjectPick) => void; onClose: () => void;
  /** editing actions; absent when the server cannot edit */
  edit?: InspectorEdit;
}) {
  const oneLayer = selection.length > 1 && selection.every((p) => p.layerId === selection[0].layerId)
    ? layers.get(selection[0].layerId) : undefined;
  const groups = new Set(selection.map((p) => p.group));
  return (
    <>
      <div className="flex h-8 shrink-0 items-center gap-1 border-b border-border px-2">
        <h2 className="flex-1 font-semibold">Inspector</h2>
        {selection.length > 1 && <span className="text-muted-foreground">{selection.length} selected</span>}
        {selection.length > 0 && <Button variant="outline" onClick={onClear} title="Clear selection (Esc)">Clear</Button>}
        <Button size="icon" aria-label="Close inspector" title="Close (])" onClick={onClose}><X /></Button>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {edit && <datalist id="cs-attribute-names">{edit.names.map((n) => <option key={n} value={n} />)}</datalist>}
        {edit && selection.length > 1 && (
          <section className="border-b border-border bg-muted/40 p-2" data-selection-edit>
            <p className="mb-1 font-medium">All {selection.length} selected</p>
            {oneLayer && (
              <div className="grid grid-cols-[auto_1fr] items-center gap-x-3 px-1">
                <span className="text-muted-foreground">Group</span>
                <GroupSelect layer={oneLayer} value={groups.size === 1 ? selection[0].group : null} label="Group of all selected"
                             onChange={(g) => edit.onGroup(selection, g)} />
              </div>
            )}
            <AddValue names={edit.names} label="Set attribute on all selected" onAdd={(n, v) => edit.onValues(selection, { [n]: v })} />
            <div className="mt-1 flex justify-end px-1">
              <Button variant="outline" onClick={edit.onDelete}>Delete all</Button>
            </div>
          </section>
        )}
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
                <dt className="text-muted-foreground">Group</dt>
                <dd className="truncate">
                  {edit && layer ? <GroupSelect layer={layer} value={p.group} label="Group" onChange={(g) => edit.onGroup([p], g)} />
                    : group?.name ?? p.group}
                </dd>
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
                        <tr key={k} className="group hover:bg-accent">
                          <td className="truncate px-1 text-muted-foreground">{k}</td>
                          <td className="px-1 text-right font-mono tabular-nums">
                            {edit ? <ValueField value={v} label={k} onCommit={(nv) => edit.onValues([p], { [k]: nv })} /> : value(v)}
                          </td>
                          {edit && (
                            <td className="w-5">
                              <button className="invisible text-muted-foreground group-hover:visible hover:text-foreground" title={`Remove ${k}`}
                                      aria-label={`Remove ${k}`} onClick={() => edit.onValues([p], { [k]: null })}><X className="size-3.5" /></button>
                            </td>
                          )}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                ) : <p className="mt-1 px-1 text-muted-foreground">No attributes.</p>
              )}
              {edit && o && o !== "error" && <AddValue names={edit.names} label="Add attribute" onAdd={(n, v) => edit.onValues([p], { [n]: v })} />}
            </section>
          );
        })}
      </div>
    </>
  );
});
