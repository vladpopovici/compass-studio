// Editing controls over the viewer: tools, undo and redo, delete, the layer
// and group new objects go into; the fork question, the new-layer form and
// short messages.
import {
  Circle, Dot, Lasso, LoaderCircle, Lock, MousePointer2, Move, Pentagon, Redo2, Spline, Square, Trash2, Undo2, X,
} from "lucide-react";
import { useState } from "react";
import type * as React from "react";
import type { LayerInfo, Schemas } from "@/api";
import { Button } from "@/components/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { groupColor, type AnnStyle } from "@/viewer/annstyle";
import type { Tool } from "@/viewer/edits";
import type { ForkAsk, Message } from "./editing";
import { layerName } from "./LayersPanel";

export const TOOLS: { tool: Tool; label: string; key: string; icon: React.ReactNode; help: string }[] = [
  { tool: "select", label: "Select", key: "v", icon: <MousePointer2 />, help: "Click to select, Shift+click to add" },
  { tool: "modify", label: "Edit shapes", key: "e", icon: <Move />,
    help: "Edit the selection: drag a vertex, drag an edge's midpoint to add one, Alt+click a vertex to remove it, drag inside to move" },
  { tool: "polygon", label: "Polygon", key: "p", icon: <Pentagon />, help: "Click vertices; double-click, Enter or click the first vertex to finish; Backspace removes the last" },
  { tool: "lasso", label: "Freehand", key: "d", icon: <Lasso />, help: "Drag to trace an outline" },
  { tool: "rectangle", label: "Rectangle", key: "r", icon: <Square />, help: "Drag from corner to corner" },
  { tool: "ellipse", label: "Ellipse", key: "c", icon: <Circle />, help: "Drag across its bounding box" },
  { tool: "point", label: "Point", key: "x", icon: <Dot />, help: "Click to place" },
  { tool: "line", label: "Line", key: "n", icon: <Spline />, help: "Click vertices; double-click or Enter to finish; Backspace removes the last" },
];

function Tip({ label, keys, children }: { label: React.ReactNode; keys?: string; children: React.ReactNode }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>{children}</TooltipTrigger>
      <TooltipContent className="max-w-72">{label}{keys && <kbd className="ml-2 font-mono text-muted-foreground">{keys}</kbd>}</TooltipContent>
    </Tooltip>
  );
}

const NEW = "__new__";

/** Single keys are tool shortcuts: keep a focused select from jumping to an item by type-ahead. */
const noTypeahead = (e: React.KeyboardEvent) => {
  if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) e.preventDefault();
};

export function EditBar(props: {
  tool: Tool; onTool: (t: Tool) => void;
  canUndo: boolean; canRedo: boolean; onUndo: () => void; onRedo: () => void;
  canDelete: boolean; onDelete: () => void;
  /** edits made since the slide was opened (reverted by "Discard changes") */
  sessionEdits: number; onDiscard: () => void;
  layers: { info: LayerInfo; style: AnnStyle }[];
  target: { layerId: string; group: number } | null; onTarget: (t: { layerId: string; group: number }) => void;
  onNewLayer: () => void; canCreate: boolean;
  /** the working layer's chip, if one is locked */
  work?: React.ReactNode;
}) {
  const t = props.layers.find((l) => l.info.id === props.target?.layerId);
  return (
    <div className="absolute top-2 left-1/2 z-10 flex w-max max-w-[calc(100%-1rem)] -translate-x-1/2 flex-wrap items-center justify-center gap-0.5 rounded-md border border-border bg-background/90 p-0.5 shadow"
         role="toolbar" aria-label="Edit tools">
      {TOOLS.map((x) => (
        <Tip key={x.tool} label={<><b>{x.label}</b><br />{x.help}</>} keys={x.key.toUpperCase()}>
          <Button size="icon" aria-label={x.label} aria-pressed={props.tool === x.tool} data-tool={x.tool}
                  className={props.tool === x.tool ? "bg-primary text-primary-foreground hover:bg-primary/90" : undefined}
                  onClick={() => props.onTool(x.tool)}>{x.icon}</Button>
        </Tip>
      ))}
      <div className="mx-1 h-4 w-px bg-border" />
      <Tip label="Undo" keys="Ctrl+Z"><Button size="icon" aria-label="Undo" disabled={!props.canUndo} onClick={props.onUndo}><Undo2 /></Button></Tip>
      <Tip label="Redo" keys="Ctrl+Shift+Z"><Button size="icon" aria-label="Redo" disabled={!props.canRedo} onClick={props.onRedo}><Redo2 /></Button></Tip>
      <Tip label="Delete the selection" keys="Del"><Button size="icon" aria-label="Delete selection" disabled={!props.canDelete} onClick={props.onDelete}><Trash2 /></Button></Tip>
      <Tip label={props.sessionEdits ? `Revert your ${props.sessionEdits} change${props.sessionEdits === 1 ? "" : "s"} since opening this slide (Redo brings them back)` : "No changes since opening this slide"}>
        <Button variant="outline" className="h-7 px-2" disabled={!props.sessionEdits} onClick={props.onDiscard}>Discard changes</Button>
      </Tip>
      <div className="mx-1 h-4 w-px bg-border" />
      <span className="px-1 text-muted-foreground">Into</span>
      <Select value={props.target?.layerId ?? ""}
              onValueChange={(v) => (v === NEW ? props.onNewLayer() : props.onTarget({ layerId: v, group: 0 }))}>
        <SelectTrigger className="h-7 w-36" aria-label="Layer for new objects" onKeyDown={noTypeahead}><SelectValue placeholder="Layer" /></SelectTrigger>
        <SelectContent>
          {props.layers.filter((l) => l.info.groups.length).map((l) => (
            <SelectItem key={l.info.id} value={l.info.id}>
              {layerName(l.info)}{!l.info.editable && <span className="ml-1 text-muted-foreground">(read-only)</span>}
            </SelectItem>
          ))}
          {props.canCreate && <SelectItem value={NEW}>New layer…</SelectItem>}
        </SelectContent>
      </Select>
      <Select value={t ? String(props.target!.group) : ""} disabled={!t}
              onValueChange={(v) => props.onTarget({ layerId: props.target!.layerId, group: +v })}>
        <SelectTrigger className="h-7 w-28" aria-label="Group for new objects" onKeyDown={noTypeahead}><SelectValue placeholder="Group" /></SelectTrigger>
        <SelectContent>
          {t?.info.groups.map((g) => (
            <SelectItem key={g.index} value={String(g.index)}>
              <span className="mr-1 inline-block size-2.5 rounded-sm" style={{ background: groupColor(t.style, g.index) }} />
              {g.name}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      {props.work && <><div className="mx-1 h-4 w-px bg-border" />{props.work}</>}
    </div>
  );
}

/** "Working in: layer", with a button to release it. */
export function WorkChip({ layer, onRelease }: { layer: string; onRelease: () => void }) {
  return (
    <span className="flex h-7 items-center gap-1 rounded border border-primary/60 bg-accent pr-0.5 pl-1.5" data-working-chip>
      <Lock className="size-3.5 text-primary" />
      <span className="text-muted-foreground">Working in</span>
      <span className="max-w-48 truncate" title={layer}>{layer}</span>
      <Tip label="Release the working layer" keys="G">
        <Button size="icon" className="size-6" aria-label="Release working layer" onClick={onRelease}><X /></Button>
      </Tip>
    </span>
  );
}

function Modal({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50" role="dialog" aria-modal="true" aria-label={title}>
      <div className="w-96 rounded-md border border-border bg-background p-4 shadow-lg">
        <h2 className="mb-2 font-semibold">{title}</h2>
        {children}
      </div>
    </div>
  );
}

export function DiscardDialog({ count, layer, onConfirm, onCancel }: {
  count: number; layer?: string; onConfirm: () => void; onCancel: () => void;
}) {
  return (
    <Modal title={layer ? "Discard edits" : "Discard changes"}>
      <p className="mb-3 text-muted-foreground">
        {layer
          ? <>Revert <b className="text-foreground">{layer}</b> to its last save point ({count} edit{count === 1 ? "" : "s"},
              by anyone)? Redo (Ctrl+Shift+Z) brings them back.</>
          : <>Revert your {count} change{count === 1 ? "" : "s"} since this slide was opened? This is saved like any
              other edit: Redo (Ctrl+Shift+Z) brings them back. Changes by others are kept.</>}
      </p>
      <div className="flex justify-end gap-2">
        <Button variant="outline" onClick={onCancel}>Cancel</Button>
        <Button className="bg-red-600 text-white hover:bg-red-600/90" autoFocus onClick={onConfirm}>Discard</Button>
      </div>
    </Modal>
  );
}

export function ForkDialog({ ask }: { ask: ForkAsk }) {
  const n = Object.values(ask.layer.counts).reduce((a, b) => a + b, 0);
  return (
    <Modal title="Edit a read-only layer">
      <p className="mb-3 text-muted-foreground">
        <b className="text-foreground">{ask.layer.name}</b> comes from the annotation store and is not changed by the
        viewer. Edits go to an editable copy in the curation database; the original stays as it is.
        {" "}The copy, <b className="text-foreground">{ask.layer.name} (editable)</b>, takes the layer's place in the list;
        "Save edits" on it makes it the layer from then on.
        {!ask.existing && n > 100000 && <> Copying {n.toLocaleString()} objects takes a few seconds.</>}
      </p>
      <div className="flex justify-end gap-2">
        <Button variant="outline" onClick={() => ask.answer(null)}>Cancel</Button>
        <Button className="bg-primary text-primary-foreground hover:bg-primary/90" autoFocus
                onClick={() => ask.answer(ask.existing ? "existing" : "new")}>
          {ask.existing ? "Edit the copy" : "Make an editable copy"}
        </Button>
      </div>
    </Modal>
  );
}

export function NewLayerDialog({ stores, initial, onCreate, onCancel }: {
  stores: Schemas["Store"][]; initial?: string; onCreate: (store: string, name: string, groups: string[]) => void; onCancel: () => void;
}) {
  const [store, setStore] = useState(stores.find((s) => s.id === initial)?.id ?? stores[0]?.id ?? "");
  const [name, setName] = useState("");
  const [groups, setGroups] = useState("");
  const list = [...new Set(groups.split(",").map((g) => g.trim()).filter(Boolean))];
  const ok = !!store && name.trim() !== "" && list.length > 0;
  return (
    <Modal title="New annotation layer">
      <form className="flex flex-col gap-2" onSubmit={(e) => {
        e.preventDefault();
        if (ok) onCreate(store, name.trim(), list);
      }}>
        {stores.length > 1 && (
          <label className="flex items-center gap-2">
            <span className="w-16 text-muted-foreground">Store</span>
            <select value={store} onChange={(e) => setStore(e.target.value)} aria-label="Store" className="flex-1 rounded border border-border bg-background px-1 py-0.5">
              {stores.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
            </select>
          </label>
        )}
        <label className="flex items-center gap-2">
          <span className="w-16 text-muted-foreground">Name</span>
          <input autoFocus value={name} onChange={(e) => setName(e.target.value)} aria-label="Layer name"
                 className="flex-1 rounded border border-border bg-background px-1 py-0.5" />
        </label>
        <label className="flex items-center gap-2">
          <span className="w-16 text-muted-foreground">Groups</span>
          <input value={groups} onChange={(e) => setGroups(e.target.value)} placeholder="tumour, stroma, necrosis" aria-label="Group names"
                 className="flex-1 rounded border border-border bg-background px-1 py-0.5" />
        </label>
        <p className="text-muted-foreground">Group names separated by commas, in order.</p>
        <div className="flex justify-end gap-2">
          <Button type="button" variant="outline" onClick={onCancel}>Cancel</Button>
          <Button type="submit" disabled={!ok} className="bg-primary text-primary-foreground hover:bg-primary/90">Create</Button>
        </div>
      </form>
    </Modal>
  );
}

export function Busy({ text }: { text: string }) {
  return (
    <Modal title="Working">
      <p className="flex items-center gap-2"><LoaderCircle className="size-4 animate-spin" />{text}</p>
    </Modal>
  );
}

export function Toast({ message }: { message: Message }) {
  return (
    <div role="status" data-message={message.error ? "error" : "info"}
         className={`absolute bottom-3 left-1/2 z-10 -translate-x-1/2 rounded-md border px-3 py-1.5 shadow ${
           message.error ? "border-red-500/60 bg-red-950/90 text-red-100" : "border-border bg-background/90"}`}>
      {message.text}
    </div>
  );
}
