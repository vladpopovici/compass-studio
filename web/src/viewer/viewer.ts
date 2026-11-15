// Framework-free slide viewer: a deck.gl OrthographicView over level-0 pixel
// coordinates (y down) with the image tile layer. The React shell and the
// spike benches drive it through this class only.

import { Deck, LinearInterpolator, OrthographicView, type OrthographicViewState } from "@deck.gl/core";
import type { ImageInfo } from "../api";
import type { Layer } from "@deck.gl/core";
import { LabelSource, type LabelStyle } from "./labels";
import {
  ASSUMED_MPP, bandAt, selectionLayers, type AnnSource, type AnnStyle, type BandId, type ObjectPick,
} from "./annstyle";
import { fitZoom, makeTileLayer, TILE_PRESETS, TileStats, type TileOptions } from "./tiles";

export interface View {
  /** level-0 px at the centre of the canvas */
  x: number;
  y: number;
  /** log2 of screen px per level-0 px (0 = 1:1) */
  zoom: number;
  /** canvas size in CSS px */
  width: number;
  height: number;
}

export interface ViewerOptions {
  onView?: (v: View) => void;
  /** cursor in level-0 px, and the annotation object under it */
  onHover?: (p: [number, number] | null, pick: ObjectPick | null) => void;
  /** click: the object under the cursor (null: none); `add` with Shift */
  onClick?: (pick: ObjectPick | null, add: boolean) => void;
  tiles?: TileOptions;
  /** zoom limits relative to 1:1; the lower limit is also bounded by fit - 1 */
  maxZoom?: number;
}

const ANIMATE_MS = 180;

export class Viewer {
  readonly stats = new TileStats();
  /** frames rendered so far (for measurements) */
  frames = 0;
  private deck: Deck<OrthographicView>;
  private info: ImageInfo | null = null;
  private vs: OrthographicViewState = { target: [0, 0, 0], zoom: 0 };
  private size: [number, number] = [1, 1];
  private resize: ResizeObserver;
  private imageLayer: Layer | null = null;
  private labels = new Map<string, { source: LabelSource; style: LabelStyle }>();
  private annotations: { source: AnnSource; style: AnnStyle }[] = [];
  private selection: ObjectPick[] = [];
  /** size band at the current zoom; layers are rebuilt when it changes */
  private band: BandId | null = null;
  private renderQueued = false;

  constructor(parent: HTMLDivElement, private opts: ViewerOptions = {}) {
    this.deck = new Deck({
      parent,
      views: new OrthographicView({ id: "ortho", flipY: true }),
      viewState: this.vs,
      controller: { inertia: 300, scrollZoom: { speed: 0.02, smooth: true }, keyboard: false, doubleClickZoom: true },
      onViewStateChange: ({ viewState }) => {
        this.setViewState(viewState as OrthographicViewState);
      },
      onHover: (info) => this.opts.onHover?.(info.coordinate ? [info.coordinate[0], info.coordinate[1]] : null,
                                              this.objectPick(info.layer?.id, info.index)),
      onClick: (info, ev) => this.opts.onClick?.(this.objectPick(info.layer?.id, info.index),
                                                 !!(ev.srcEvent as MouseEvent | undefined)?.shiftKey),
      onAfterRender: () => {
        this.frames++;
        this.stats.markDrawn(performance.now());
      },
      getCursor: ({ isDragging, isHovering }) => (isDragging ? "grabbing" : isHovering ? "pointer" : "crosshair"),
    });
    this.resize = new ResizeObserver(() => {
      const r = parent.getBoundingClientRect();
      this.size = [Math.max(1, r.width), Math.max(1, r.height)];
      this.setViewState(this.vs);
    });
    this.resize.observe(parent);
    const r = parent.getBoundingClientRect();
    this.size = [Math.max(1, r.width), Math.max(1, r.height)];
  }

  get image() {
    return this.info;
  }

  /** Show an image; `view` restores a position (else fit). */
  setImage(info: ImageInfo | null, view?: Partial<Pick<View, "x" | "y" | "zoom">>) {
    this.info = info;
    this.imageLayer = info ? makeTileLayer(info, this.stats, "", this.opts.tiles ?? TILE_PRESETS.tuned) : null;
    this.render();
    if (!info) return;
    const fit = this.fitZoom();
    this.setViewState({
      target: [view?.x ?? info.width / 2, view?.y ?? info.height / 2, 0],
      zoom: view?.zoom ?? fit,
    });
  }

  /** Label maps drawn over the image, bottom first; sources are kept while listed. */
  setLabels(entries: { info: ImageInfo; style: LabelStyle }[]) {
    const keep = new Set(entries.map((e) => e.info.id));
    for (const [id, l] of this.labels)
      if (!keep.has(id) || l.source.info.version !== entries.find((e) => e.info.id === id)!.info.version) {
        l.source.destroy();
        this.labels.delete(id);
      }
    const next = new Map<string, { source: LabelSource; style: LabelStyle }>();
    for (const { info, style } of entries) {
      const source = this.labels.get(info.id)?.source ?? new LabelSource(info, this.opts.tiles ?? TILE_PRESETS.tuned);
      source.setStyle(style);
      next.set(info.id, { source, style });
    }
    this.labels = next;
    this.render();
  }

  /** Label values under a level-0 point, for visible maps (top first). */
  labelsAt(x: number, y: number): { info: ImageInfo; value: number }[] {
    const level = Math.max(0, Math.floor(-(this.vs.zoom as number) + 1e-9));
    const out: { info: ImageInfo; value: number }[] = [];
    for (const { source, style } of [...this.labels.values()].reverse()) {
      if (!style.visible) continue;
      const v = source.valueAt(x, y, level);
      if (v !== null) out.push({ info: source.info, value: v });
    }
    return out;
  }

  /** Annotation sources drawn above the label maps, bottom first. */
  setAnnotations(entries: { source: AnnSource; style: AnnStyle }[]) {
    const known = new Set(this.annotations.map((e) => e.source));
    this.annotations = entries;
    for (const { source } of entries) {
      if (known.has(source)) continue;
      source.onChange = () => this.queueRender();
      if (this.info) source.setView?.(this.view(), this.info);
    }
    this.render();
  }

  /** Selected objects, outlined. */
  setSelection(picks: ObjectPick[]) {
    this.selection = picks;
    this.render();
  }

  private objectPick(deckLayerId: string | undefined, index: number): ObjectPick | null {
    if (!deckLayerId || index < 0) return null;
    for (const { source } of this.annotations) {
      const p = source.pick(deckLayerId, index);
      if (p) return p;
    }
    return null;
  }

  /** Object under a canvas position (CSS px), as hover would pick it; for measurements. */
  pickAt(x: number, y: number): ObjectPick | null {
    const info = this.deck.pickObject({ x, y, radius: 0 });
    return info ? this.objectPick(info.layer?.id, info.index) : null;
  }

  labelSource(id: string): LabelSource | undefined {
    return this.labels.get(id)?.source;
  }

  private render() {
    const layers: Layer[] = this.imageLayer ? [this.imageLayer] : [];
    for (const { source, style } of this.labels.values()) layers.push(source.layer(style));
    const mpp = this.info?.mpp || ASSUMED_MPP, zoom = this.vs.zoom as number;
    const band = (this.band = bandAt(mpp, zoom).id);
    for (const { source, style } of this.annotations) layers.push(...source.layers({ style, mpp, band, zoom }));
    if (this.selection.length) {
      const size = Math.max(...this.annotations.map((p) => p.style.sizes[band]), 0);
      layers.push(...selectionLayers(this.selection, size, mpp));
    }
    this.deck.setProps({ layers });
  }

  /** Render on the next frame (sources changing data in bursts). */
  private queueRender() {
    if (this.renderQueued) return;
    this.renderQueued = true;
    requestAnimationFrame(() => {
      this.renderQueued = false;
      this.render();
    });
  }

  view(): View {
    const [x, y] = this.vs.target as number[];
    return { x, y, zoom: this.vs.zoom as number, width: this.size[0], height: this.size[1] };
  }

  fitZoom(): number {
    return this.info ? fitZoom(this.info, this.size[0] * 0.95, this.size[1] * 0.95) : 0;
  }

  fit() {
    if (!this.info) return;
    this.animate([this.info.width / 2, this.info.height / 2], this.fitZoom());
  }

  zoomTo(zoom: number, around?: [number, number]) {
    const v = this.view();
    let target: [number, number] = [v.x, v.y];
    if (around) {
      // keep the level-0 point `around` fixed on screen
      const k = 2 ** (v.zoom - this.clampZoom(zoom));
      target = [around[0] + (v.x - around[0]) * k, around[1] + (v.y - around[1]) * k];
    }
    this.animate(target, zoom);
  }

  zoomBy(delta: number) {
    this.zoomTo((this.vs.zoom as number) + delta);
  }

  /** Pan by a fraction of the canvas size. */
  panBy(fx: number, fy: number) {
    const v = this.view();
    const s = 2 ** v.zoom;
    this.animate([v.x + (fx * v.width) / s, v.y + (fy * v.height) / s], v.zoom);
  }

  destroy() {
    for (const l of this.labels.values()) l.source.destroy();
    for (const a of this.annotations) a.source.destroy?.();
    this.resize.disconnect();
    this.deck.finalize();
  }

  private clampZoom(z: number) {
    const lo = this.fitZoom() - 1, hi = this.opts.maxZoom ?? 6;
    return Math.min(hi, Math.max(Math.min(lo, hi), z));
  }

  private animate(target: [number, number], zoom: number) {
    this.setViewState({
      target: [target[0], target[1], 0], zoom,
      transitionDuration: ANIMATE_MS,
      transitionInterpolator: new LinearInterpolator(["target", "zoom"]),
    });
  }

  private setViewState(v: OrthographicViewState) {
    // Change deck's objects only where clamping changes a value: during a
    // transition deck compares the states we hand back with its own
    // (target arrays included) and restarts the transition on any difference.
    const zoom = this.clampZoom(v.zoom as number);
    const t = v.target as number[];
    let [x, y] = t;
    if (this.info) {
      // the view centre stays on the image
      x = Math.min(this.info.width, Math.max(0, x));
      y = Math.min(this.info.height, Math.max(0, y));
    }
    const target = (x === t[0] && y === t[1] ? t : t.length > 2 ? [x, y, t[2]] : [x, y]) as [number, number];
    const minZoom = this.clampZoom(-Infinity), maxZoom = this.clampZoom(Infinity);
    this.vs = target === t && zoom === v.zoom && v.minZoom === minZoom && v.maxZoom === maxZoom
      ? v : { ...v, target, zoom, minZoom, maxZoom };
    this.deck.setProps({ viewState: this.vs });
    if (this.info) for (const { source } of this.annotations) source.setView?.(this.view(), this.info);
    if (this.annotations.length && bandAt(this.info?.mpp || ASSUMED_MPP, zoom).id !== this.band) this.render();
    this.opts.onView?.(this.view());
  }
}
