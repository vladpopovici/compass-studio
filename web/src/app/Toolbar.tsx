import { Layers, Maximize, Ratio, ZoomIn, ZoomOut } from "lucide-react";
import type * as React from "react";
import type { ImageInfo } from "@/api";
import { Button } from "@/components/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { View } from "@/viewer/viewer";
import { magnification, zoomPercent } from "./format";

function Tip({ label, keys, children }: { label: string; keys?: string; children: React.ReactNode }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>{children}</TooltipTrigger>
      <TooltipContent>{label}{keys && <kbd className="ml-2 font-mono text-muted-foreground">{keys}</kbd>}</TooltipContent>
    </Tooltip>
  );
}

export function Toolbar(props: {
  images: ImageInfo[]; image: ImageInfo | null; view: View | null; onImage: (id: string) => void;
  onZoomBy: (d: number) => void; onFit: () => void; onOneToOne: () => void;
  labelCount: number; panel: boolean; onPanel: () => void;
}) {
  const { images, image, view } = props;
  const mag = image && view ? magnification(image.objective_power, view.zoom) : null;
  return (
    <header className="flex h-9 shrink-0 items-center gap-1 border-b border-border px-2">
      <span className="mr-2 font-semibold tracking-tight">CompassStudio</span>
      <Select value={image?.id ?? ""} onValueChange={props.onImage} disabled={!images.length}>
        <SelectTrigger className="w-56" aria-label="Image"><SelectValue placeholder="No image" /></SelectTrigger>
        <SelectContent>
          {images.map((i) => (
            <SelectItem key={i.id} value={i.id}>
              {i.name ?? i.id}
              <span className="ml-2 text-muted-foreground">{i.width.toLocaleString()} × {i.height.toLocaleString()}</span>
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      <div className="mx-2 h-4 w-px bg-border" />
      <Tip label="Zoom out" keys="−"><Button size="icon" aria-label="Zoom out" onClick={() => props.onZoomBy(-1)}><ZoomOut /></Button></Tip>
      <span className="w-24 text-center font-mono tabular-nums" title="Screen px per level-0 px; apparent magnification">
        {image && view ? zoomPercent(view.zoom) : "–"}{mag && <span className="text-muted-foreground"> {mag}</span>}
      </span>
      <Tip label="Zoom in" keys="+"><Button size="icon" aria-label="Zoom in" onClick={() => props.onZoomBy(1)}><ZoomIn /></Button></Tip>
      <Tip label="Fit to window" keys="0"><Button size="icon" aria-label="Fit" onClick={props.onFit}><Maximize /></Button></Tip>
      <Tip label="Actual pixels (1:1)" keys="1"><Button size="icon" aria-label="1:1" onClick={props.onOneToOne}><Ratio /></Button></Tip>
      <div className="flex-1" />
      {props.labelCount > 0 && (
        <Tip label="Label maps panel (l: show or hide maps, o: outlines)">
          <Button aria-label="Label maps" aria-pressed={props.panel} onClick={props.onPanel}
                  className={props.panel ? "text-foreground" : undefined}>
            <Layers />Labels <span className="text-muted-foreground">{props.labelCount}</span>
          </Button>
        </Tip>
      )}
    </header>
  );
}
