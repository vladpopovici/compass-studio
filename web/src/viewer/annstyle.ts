// Shared by the annotation sources (points, polygons, lines): per-layer
// style, group colours, size bands, picks, the selection overlay and the
// interface the viewer draws them through.
//
// Sizes are physical: point diameters and line widths are in microns, drawn
// true to size. One size cannot suit every zoom (on a 0.47 µm/px slide,
// 50 µm is 107 px at 1:1 and under 1 px at fit), so each is chosen per
// resolution band (BANDS, by µm per screen pixel): each band has its own
// slider range and keeps its own value, and sizes switch when the zoom
// crosses into another band.

import type { Layer } from "@deck.gl/core";
import { PathLayer, ScatterplotLayer } from "@deck.gl/layers";
import type { ImageInfo } from "../api";
import type { View } from "./viewer";

export type BandId = "high" | "medium" | "low";

/** Resolution bands, finest first: a band applies while µm per screen
 * pixel is below `below`. On a 0.47 µm/px slide: high = levels 0 to 2,
 * medium = 3 and 4, low = 5 and above (and the fitted view). */
export const BANDS: { id: BandId; below: number; name: string }[] = [
  { id: "high", below: 2.5, name: "high magnification" },
  { id: "medium", below: 12, name: "medium magnification" },
  { id: "low", below: Infinity, name: "low magnification" },
];

export interface Range { min: number; max: number; def: number }

/** point (and polygon centre) diameters, µm */
export const POINT_SIZES: Record<BandId, Range> = {
  high: { min: 5, max: 100, def: 20 },
  medium: { min: 20, max: 200, def: 60 },
  low: { min: 100, max: 1000, def: 300 },
};

/** polygon outline and polyline widths, µm */
export const LINE_WIDTHS: Record<BandId, Range> = {
  high: { min: 0.5, max: 10, def: 1.5 },
  medium: { min: 2, max: 40, def: 6 },
  low: { min: 10, max: 200, def: 40 },
};

const defaults = (r: Record<BandId, Range>) =>
  Object.fromEntries(BANDS.map((b) => [b.id, r[b.id].def])) as Record<BandId, number>;

export interface AnnStyle {
  visible: boolean;
  /** group indices not drawn */
  hidden: number[];
  /** "#rrggbb" per group index; missing entries use the default palette */
  colors: Record<number, string>;
  /** whole layer */
  opacity: number;
  /** point and polygon-centre diameter per band, µm */
  sizes: Record<BandId, number>;
  /** polygon outline and polyline width per band, µm */
  widths: Record<BandId, number>;
  /** polygon fill, 0 to 1 (0: outlines only) */
  fillOpacity: number;
  outline: boolean;
  /** polygons whose bounding box is at least this many screen px on a side (by area) are shapes, smaller ones centres */
  minShapePx: number;
}

/** range of `minShapePx` */
export const MIN_SHAPE_PX: Range = { min: 2, max: 64, def: 8 };

export const DEFAULT_ANN_STYLE: AnnStyle = {
  visible: true, hidden: [], colors: {}, opacity: 0.9,
  sizes: defaults(POINT_SIZES), widths: defaults(LINE_WIDTHS), fillOpacity: 0.3, outline: true,
  minShapePx: MIN_SHAPE_PX.def,
};

/** used when the image has no calibration */
export const ASSUMED_MPP = 0.5;

// Okabe-Ito, then Tableau 10 (colour-blind friendly first)
export const GROUP_PALETTE = [
  "#e69f00", "#56b4e9", "#009e73", "#f0e442", "#0072b2", "#d55e00", "#cc79a7",
  "#4e79a7", "#f28e2b", "#e15759", "#76b7b2", "#59a14f", "#edc948", "#b07aa1", "#ff9da7", "#9c755f",
];

export function groupColor(style: AnnStyle, index: number): string {
  return style.colors[index] ?? GROUP_PALETTE[index % GROUP_PALETTE.length];
}

export function rgb(hex: string): [number, number, number] {
  const n = parseInt(hex.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

/** Microns per screen pixel at a zoom (deck zoom: 0 = 1:1, -1 = 1:2). */
export function umPerScreenPx(mpp: number, zoom: number): number {
  return mpp / 2 ** zoom;
}

export function bandAt(mpp: number, zoom: number) {
  const s = umPerScreenPx(mpp, zoom);
  return BANDS.find((b) => s < b.below) ?? BANDS[BANDS.length - 1];
}

/** Pyramid level shown at a zoom (0 = full resolution). */
export function levelAt(zoom: number): number {
  return Math.max(0, Math.floor(-zoom + 1e-9));
}

export type ObjectKind = "point" | "polygon" | "polyline";

export interface ObjectPick {
  layerId: string;
  objectId: number;
  group: number;
  kind: ObjectKind;
  /** level-0 px: the point, or the centre of the shape's bounding box */
  x: number;
  y: number;
  /** outline of a polygon (rings, closed) or polyline (open), for the selection overlay */
  shape?: { paths: Float32Array[]; closed: boolean };
}

export interface DrawContext {
  style: AnnStyle;
  mpp: number;
  band: BandId;
  zoom: number;
}

/** What the viewer draws: one annotation layer's objects of one kind. */
export interface AnnSource {
  /** contract layer id */
  readonly layerId: string;
  /** deck layers for the current view */
  layers(ctx: DrawContext): Layer[];
  /** the object behind a picked deck layer and index, if that layer is one of ours */
  pick(deckLayerId: string, index: number): ObjectPick | null;
  /** view or style changes (tiled sources fetch here) */
  setView?(view: View, image: ImageInfo, style: AnnStyle): void;
  /** set by the viewer: call when what `layers` returns has changed */
  onChange?: () => void;
  destroy?(): void;
}

/** Selected objects drawn above everything: a dark line under a white one,
 * visible on light and dark colours alike. Points get a ring a little larger
 * than `pointUm` (the largest point size in use), shapes their outline. */
export function selectionLayers(picks: ObjectPick[], pointUm: number, mpp: number): Layer[] {
  const dots = picks.filter((p) => !p.shape);
  const paths = picks.flatMap((p) => p.shape ? p.shape.paths.map((path) => ({ path, closed: p.shape!.closed })) : []);
  const r = (pointUm / 2 / mpp) * 1.3;
  const out: Layer[] = [];
  for (const [name, color, width] of [["dark", [0, 0, 0, 255], 4], ["light", [255, 255, 255, 255], 2]] as const) {
    if (dots.length)
      out.push(new ScatterplotLayer<ObjectPick>({
        id: `selection-dots-${name}`, data: dots, getPosition: (p) => [p.x, p.y],
        radiusUnits: "common", getRadius: r, radiusMinPixels: 5, stroked: true, filled: false,
        lineWidthUnits: "pixels", getLineWidth: width, getLineColor: [...color], updateTriggers: { getRadius: r },
      }));
    if (paths.length)
      out.push(new PathLayer<{ path: Float32Array; closed: boolean }>({
        id: `selection-paths-${name}`, data: paths,
        getPath: (d) => (d.closed ? closeRing(d.path) : d.path) as never, positionFormat: "XY",
        widthUnits: "pixels", getWidth: width, getColor: [...color], jointRounded: true,
      }));
  }
  return out;
}

function closeRing(p: Float32Array): Float32Array {
  const out = new Float32Array(p.length + 2);
  out.set(p);
  out[p.length] = p[0];
  out[p.length + 1] = p[1];
  return out;
}
