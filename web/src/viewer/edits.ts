// Editing in the viewer (milestone 7, decision D8).
//
// Bulk layers stay as fetched. Objects changed since (by this client or by
// any other writer of the sidecar) are hidden in them and drawn here, from
// their current GeoJSON, one OverlaySource per editable layer. Objects being
// edited are hidden in both and live in the editor layer
// (editable-layers' EditableGeoJsonLayer, CARTESIAN coordinates, S5).

import { COORDINATE_SYSTEM, type Layer } from "@deck.gl/core";
import {
  CompositeMode, DrawLineStringMode, DrawPointMode, DrawPolygonByDraggingMode, DrawPolygonMode,
  DrawRectangleMode, EditableGeoJsonLayer, ModifyMode, TranslateMode,
} from "@deck.gl-community/editable-layers";
import { GeoJsonLayer } from "@deck.gl/layers";
import type { LayerInfo } from "../api";
import { groupColor, rgb, type AnnSource, type AnnStyle, type DrawContext, type ObjectPick } from "./annstyle";
import { centreOf, kindOf, outlineOf, type Geometry } from "./geometry";

/** An object's current state, as drawn by the overlay or edited. */
export interface EditFeature {
  id: number;
  group: number;
  geometry: Geometry;
}

export type Tool = "select" | "modify" | "polygon" | "lasso" | "rectangle" | "ellipse" | "point" | "line";
export const DRAW_TOOLS: ReadonlySet<Tool> = new Set(["polygon", "lasso", "rectangle", "ellipse", "point", "line"]);

/** Changed objects of one editable layer, drawn over its (hiding) bulk layers. */
export class OverlaySource implements AnnSource {
  onChange?: () => void;
  private features: EditFeature[] = [];
  private shown: EditFeature[] = [];
  private data: object | null = null;
  private dataKey = "";

  constructor(readonly info: LayerInfo) {}

  get layerId() {
    return this.info.id;
  }

  get deckId() {
    return `overlay-${this.info.id}`;
  }

  setFeatures(f: EditFeature[]) {
    this.features = f;
    this.data = null;
    this.onChange?.();
  }

  get count() {
    return this.features.length;
  }

  layers(ctx: DrawContext): Layer[] {
    const { style } = ctx;
    if (!style.visible || !this.features.length) return [];
    const key = style.hidden.join(",");
    if (!this.data || key !== this.dataKey) {
      const hidden = new Set(style.hidden);
      this.shown = this.features.filter((f) => !hidden.has(f.group));
      this.data = { type: "FeatureCollection", features: this.shown.map((f) => ({ type: "Feature", id: f.id, geometry: f.geometry, properties: f })) };
      this.dataKey = key;
    }
    return [this.layer(ctx, this.data)];
  }

  private layer({ style, mpp, band }: DrawContext, data: object) {
    const colors = this.info.groups.map((g) => rgb(groupColor(style, g.index)));
    const colorOf = (f: { properties: EditFeature }) => colors[f.properties.group] ?? [128, 128, 128];
    return new GeoJsonLayer({
      id: this.deckId,
      data: data as never,
      coordinateSystem: COORDINATE_SYSTEM.CARTESIAN,
      opacity: style.opacity,
      filled: true,
      stroked: true,
      pointType: "circle",
      getFillColor: (f) => {
        const ft = f as unknown as { properties: EditFeature };
        return [...colorOf(ft), ft.properties.geometry.type === "Point" ? 255 : Math.round(255 * style.fillOpacity)];
      },
      getLineColor: (f) => {
        const ft = f as unknown as { properties: EditFeature };
        const t = ft.properties.geometry.type;
        return [...colorOf(ft), t === "Point" || (t === "Polygon" && !style.outline) ? 0 : 255];
      },
      lineWidthUnits: "common",
      getLineWidth: style.widths[band] / mpp,
      lineWidthMinPixels: 1,
      lineCapRounded: true,
      lineJointRounded: true,
      pointRadiusUnits: "common",
      getPointRadius: style.sizes[band] / 2 / mpp,
      pointRadiusMinPixels: 1,
      pickable: true,
      autoHighlight: true,
      highlightColor: [255, 255, 255, 120],
      updateTriggers: {
        getFillColor: [colors.flat().join(), style.fillOpacity],
        getLineColor: [colors.flat().join(), style.outline],
      },
    });
  }

  pick(deckLayerId: string, index: number): ObjectPick | null {
    if (deckLayerId !== this.deckId) return null;
    const f = this.shown[index];
    return f ? featurePick(this.info.id, f) : null;
  }
}

export function featurePick(layerId: string, f: EditFeature): ObjectPick {
  const [x, y] = centreOf(f.geometry);
  return { layerId, objectId: f.id, group: f.group, kind: kindOf(f.geometry), x, y, shape: outlineOf(f.geometry) };
}

/** Is a pick (editable-layers) one of the editor's handles? */
type EditPick = { isGuide?: boolean; object?: { properties?: { editHandleType?: string } } };

/** Vertex editing: drag a vertex, drag or click an edge midpoint to add one, Alt+click a vertex to remove it. */
class VertexMode extends ModifyMode {
  handleClick(event: Parameters<ModifyMode["handleClick"]>[0], props: Parameters<ModifyMode["handleClick"]>[1]) {
    const existing = (event.picks as EditPick[]).some((p) => p.isGuide && p.object?.properties?.editHandleType === "existing");
    if (existing && !(event.sourceEvent as MouseEvent | undefined)?.altKey) return;
    super.handleClick(event, props);
  }
}

/** Moving whole objects: drag inside one, but not on a handle (those belong to VertexMode). */
class MoveMode extends TranslateMode {
  isSelectionPicked(picks: Parameters<TranslateMode["isSelectionPicked"]>[0], props: Parameters<TranslateMode["isSelectionPicked"]>[1]) {
    if ((picks as EditPick[] | undefined)?.some((p) => p.isGuide)) return false;
    return super.isSelectionPicked(picks, props);
  }
}

const MODES: Record<Exclude<Tool, "select">, () => object> = {
  modify: () => new CompositeMode([new VertexMode(), new MoveMode()]),
  polygon: () => new DrawPolygonMode(),
  lasso: () => new DrawPolygonByDraggingMode(),
  rectangle: () => new DrawRectangleMode(),
  ellipse: () => new DrawRectangleMode(),
  point: () => new DrawPointMode(),
  line: () => new DrawLineStringMode(),
};
const modeCache = new Map<Tool, object>();

export function modeFor(tool: Exclude<Tool, "select">): object {
  let m = modeCache.get(tool);
  if (!m) modeCache.set(tool, (m = MODES[tool]()));
  return m;
}

export interface EditEvent {
  updatedData: { type: "FeatureCollection"; features: { geometry: Geometry; properties?: Record<string, unknown> }[] };
  editType: string;
  editContext?: { featureIndexes?: number[] };
}

/** The editor layer: drawing (empty data) or editing checked-out features. */
export function editorLayer(opts: {
  tool: Exclude<Tool, "select">; features: EditFeature[]; style: AnnStyle | null; info: LayerInfo | null;
  onEdit: (e: EditEvent) => void; zoom: number;
  /** µm per level-0 px, for the length shown while drawing a line (null: px) */
  mpp: number | null;
}): Layer {
  const { tool, features, style, info } = opts;
  const colorOf = (g: number) => (style && info ? rgb(groupColor(style, g)) : [255, 255, 255]);
  const data = {
    type: "FeatureCollection",
    features: features.map((f) => ({ type: "Feature", id: f.id, geometry: f.geometry, properties: { id: f.id, group: f.group } })),
  };
  return new EditableGeoJsonLayer({
    id: "edit",
    data: data as never,
    mode: modeFor(tool) as never,
    selectedFeatureIndexes: tool === "modify" ? features.map((_, i) => i) : [],
    coordinateSystem: COORDINATE_SYSTEM.CARTESIAN,
    modeConfig: tool === "lasso" ? { throttleMs: 16 }
      : tool === "rectangle" || tool === "ellipse" ? { dragToDraw: true }
      : tool === "line" ? { formatTooltip: (d: number) => (opts.mpp ? `${(d * opts.mpp).toFixed(1)} µm` : `${d.toFixed(0)} px`) }
      : {},
    // characterSet "auto": the default atlas is ASCII only (no µ)
    _subLayerProps: { tooltips: { characterSet: "auto", getSize: 13, background: true, getBackgroundColor: [0, 0, 0, 170], getColor: [255, 255, 255, 255],
                                  getPixelOffset: [0, -14] } },
    onEdit: opts.onEdit as never,
    pickingRadius: 6,
    getFillColor: ((f: { properties?: { group?: number } }) => [...colorOf(f.properties?.group ?? 0), 70]) as never,
    getLineColor: ((f: { properties?: { group?: number } }) => [...colorOf(f.properties?.group ?? 0), 255]) as never,
    getTentativeFillColor: [255, 255, 255, 40],
    getTentativeLineColor: [255, 255, 255, 230],
    getLineWidth: 2,
    lineWidthUnits: "pixels",
    getRadius: 5,
    pointRadiusUnits: "pixels",
    editHandlePointRadius: 4,
    getEditHandlePointColor: [255, 255, 255, 255],
    getEditHandlePointOutlineColor: [20, 20, 20, 255],
  } as never);
}

/** Remove the last clicked vertex of a polygon or line being drawn; false if there is none. */
export function popVertex(tool: Tool): boolean {
  if (tool !== "polygon" && tool !== "line") return false;
  const m = modeCache.get(tool) as { getClickSequence?: () => unknown[] } | undefined;
  const seq = m?.getClickSequence?.();
  if (!seq?.length) return false;
  seq.pop();
  return true;
}
