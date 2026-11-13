// Label maps (milestone 4, D6): raw integer tiles (contract 1.2 `bin`)
// uploaded as textures and coloured on the GPU through a shared palette
// texture, so showing, hiding or recolouring classes never refetches tiles.
//
// Values are packed little-endian into r8/rg8/rgba8 textures and decoded in
// the shader. The palette is 256 x 256 RGBA: value v < 65536 is texel
// (v % 256, v / 256); larger values (instance maps) get a hashed colour.

import { BitmapLayer, type BitmapLayerProps } from "@deck.gl/layers";
import { TileLayer } from "@deck.gl/geo-layers";
import type { DefaultProps, UpdateParameters } from "@deck.gl/core";
import type { Device, Texture, TextureFormat } from "@luma.gl/core";
import { authHeaders, type ImageInfo } from "../api";
import { ringTileset, TILE_PRESETS, type TileOptions, type TileStats } from "./tiles";

/** Okabe-Ito, as COMPASS's viewer (overlay.DEFAULT_PALETTE). */
export const DEFAULT_PALETTE = ["#e69f00", "#56b4e9", "#009e73", "#f0e442", "#0072b2", "#d55e00", "#cc79a7", "#999999"];
const PALETTE_SIZE = 65536;
const BYTES: Record<string, 1 | 2 | 4> = { uint8: 1, uint16: 2, uint32: 4, int32: 4 };
const FORMAT: Record<1 | 2 | 4, TextureFormat> = { 1: "r8unorm", 2: "rg8unorm", 4: "rgba8unorm" };
const TILE = 512;

export interface LabelClass { value: number; name: string; color: string | null }

export interface LabelStyle {
  visible: boolean;
  opacity: number;
  /** draw class boundaries, with fills at `fillAlpha` */
  outline: boolean;
  /** values hidden */
  hidden: number[];
  /** show values that are not in the class table */
  others: boolean;
}

export const DEFAULT_STYLE: LabelStyle = { visible: true, opacity: 0.5, outline: false, hidden: [], others: true };

interface LabelTile { data: Uint8Array; width: number; height: number; bytes: 1 | 2 | 4 }

function hexRgb(c: string): [number, number, number] {
  const n = parseInt(c.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

/** Colour for a value without a table entry (also used by the shader for v >= 65536). */
export function hashColor(v: number): [number, number, number] {
  let h = Math.imul(v >>> 0, 2654435761) >>> 0;
  h ^= h >>> 15;
  h = Math.imul(h, 2246822519) >>> 0;
  h ^= h >>> 13;
  // keep colours away from black and white
  return [64 + (h & 127), 64 + ((h >>> 8) & 127), 64 + ((h >>> 16) & 127)];
}

export function classColor(classes: LabelClass[] | null | undefined, value: number): string {
  const i = classes?.findIndex((c) => c.value === value) ?? -1;
  if (i >= 0) return classes![i].color ?? DEFAULT_PALETTE[i % DEFAULT_PALETTE.length];
  const [r, g, b] = hashColor(value);
  return `#${((1 << 24) | (r << 16) | (g << 8) | b).toString(16).slice(1)}`;
}

/** One label map: its palette, its loaded tiles (for hover) and its layer. */
export class LabelSource {
  readonly bytes: 1 | 2 | 4;
  readonly palette = new Uint8Array(PALETTE_SIZE * 4);
  paletteVersion = 0;
  /** values outside the table (incl. >= 65536, coloured in the shader) are shown */
  others = true;
  private tiles = new Map<string, LabelTile>();
  private uploaded = -1;
  private paletteTexture: Texture | null = null;
  private styleKey = "";

  constructor(readonly info: ImageInfo, private options: TileOptions = TILE_PRESETS.tuned,
              /** optional timing of layer tiles (benches) */
              readonly stats?: TileStats, private bust = "") {
    const b = BYTES[info.dtype ?? "uint8"];
    if (!b) throw new Error(`label dtype ${info.dtype} not supported`);
    this.bytes = b;
  }

  get classes(): LabelClass[] {
    return (this.info.classes ?? []) as LabelClass[];
  }

  /** Rebuild the palette for a style; returns false if nothing changed. */
  setStyle(style: LabelStyle): boolean {
    const key = JSON.stringify([style.hidden, style.others]);
    if (key === this.styleKey) return false;
    this.styleKey = key;
    const p = this.palette;
    const hidden = new Set(style.hidden);
    const others = style.others;
    this.others = others;
    for (let v = 0; v < PALETTE_SIZE; v++) {
      const [r, g, b] = hashColor(v);
      p.set([r, g, b, others ? 255 : 0], v * 4);
    }
    this.classes.forEach((c, i) => {
      const [r, g, b] = hexRgb(c.color ?? DEFAULT_PALETTE[i % DEFAULT_PALETTE.length]);
      if (c.value < PALETTE_SIZE) p.set([r, g, b, hidden.has(c.value) ? 0 : 255], c.value * 4);
    });
    for (const v of hidden) if (v >= 0 && v < PALETTE_SIZE) p[v * 4 + 3] = 0;
    const bg = this.info.background;
    if (bg != null && bg < PALETTE_SIZE) p[bg * 4 + 3] = 0;
    this.paletteVersion++;
    return true;
  }

  /** The palette texture on `device`, uploaded if the palette changed. */
  paletteOn(device: Device): Texture {
    if (!this.paletteTexture) {
      this.paletteTexture = device.createTexture({
        format: "rgba8unorm", width: 256, height: 256, mipLevels: 1,
        sampler: { minFilter: "nearest", magFilter: "nearest" },
      });
    }
    if (this.uploaded !== this.paletteVersion) {
      this.paletteTexture.writeData(this.palette);
      this.uploaded = this.paletteVersion;
    }
    return this.paletteTexture;
  }

  destroy() {
    this.paletteTexture?.destroy();
    this.paletteTexture = null;
    this.tiles.clear();
  }

  /** Label value at a level-0 point from the finest loaded tile at or above `level`. */
  valueAt(x: number, y: number, level: number): number | null {
    const n = this.info.levels.length;
    if (x < 0 || y < 0 || x >= this.info.width || y >= this.info.height) return null;
    for (let l = Math.max(0, level); l < n; l++) {
      const px = Math.floor(x / 2 ** l), py = Math.floor(y / 2 ** l);
      const t = this.tiles.get(`${l}/${Math.floor(px / TILE)}/${Math.floor(py / TILE)}`);
      if (!t) continue;
      const i = (py % TILE) * t.width + (px % TILE);
      const d = t.data;
      if (t.bytes === 1) return d[i];
      if (t.bytes === 2) return d[2 * i] | (d[2 * i + 1] << 8);
      return (d[4 * i] | (d[4 * i + 1] << 8) | (d[4 * i + 2] << 16) | (d[4 * i + 3] << 24)) >>> 0;
    }
    return null;
  }

  /** Distinct values on the coarsest level (one tile for most maps), at most `max`. */
  async discover(max = 256): Promise<number[]> {
    const l = this.info.levels.length - 1;
    const L = this.info.levels[l];
    const seen = new Set<number>();
    for (let row = 0; row * TILE < L.height; row++)
      for (let col = 0; col * TILE < L.width; col++) {
        const t = await this.fetchTile(l, col, row);
        for (let i = 0; i < t.width * t.height && seen.size <= max; i++) {
          const d = t.data, b = t.bytes;
          seen.add(b === 1 ? d[i] : b === 2 ? d[2 * i] | (d[2 * i + 1] << 8)
            : (d[4 * i] | (d[4 * i + 1] << 8) | (d[4 * i + 2] << 16) | (d[4 * i + 3] << 24)) >>> 0);
        }
      }
    if (this.info.background != null) seen.delete(this.info.background);
    return [...seen].sort((a, b) => a - b).slice(0, max);
  }

  private async fetchTile(level: number, col: number, row: number, signal?: AbortSignal): Promise<LabelTile> {
    const v = this.bust || this.info.version;
    const q = v ? `?v=${encodeURIComponent(v)}` : "";
    const path = `images/${this.info.id}/tiles/${level}/${col}/${row}.bin${q}`;
    let data: Uint8Array;
    if (this.stats && signal) data = await this.stats.fetchBytes(path, signal);
    else {
      const r = await fetch(`api/v1/${path}`, { headers: authHeaders, signal });
      if (!r.ok) throw new Error(`label tile: ${r.status}`);
      data = new Uint8Array(await r.arrayBuffer());
    }
    const L = this.info.levels[level];
    const width = Math.min(TILE, L.width - col * TILE), height = Math.min(TILE, L.height - row * TILE);
    if (data.length !== width * height * this.bytes) throw new Error("label tile: unexpected size");
    return { data, width, height, bytes: this.bytes };
  }

  layer(style: LabelStyle, fillAlpha = 0.15) {
    const info = this.info, n = info.levels.length;
    const level = (z: number) => info.levels[Math.min(n - 1, Math.max(0, -z))];
    return new TileLayer<LabelTile>({
      id: `labels-${info.id}`,
      visible: style.visible,
      opacity: style.opacity,
      tileSize: TILE,
      extent: [0, 0, info.width, info.height],
      minZoom: -(n - 1),
      maxZoom: 0,
      refinementStrategy: "best-available",
      maxRequests: this.options.maxRequests,
      TilesetClass: ringTileset(this.options.prefetchRing,
                                (z) => Math.ceil(level(z).width / TILE), (z) => Math.ceil(level(z).height / TILE)),
      loadOptions: { worker: false },
      getTileData: async ({ index: { x, y, z }, signal }) => {
        const t = await this.fetchTile(-z, x, y, signal);
        this.tiles.set(`${-z}/${x}/${y}`, t);
        return t;
      },
      onTileUnload: (tile) => {
        const { x, y, z } = tile.index;
        this.tiles.delete(`${-z}/${x}/${y}`);
      },
      updateTriggers: { renderSubLayers: [this.paletteVersion, style.outline, style.opacity, fillAlpha] },
      renderSubLayers: (props) => {
        const t = props.data as LabelTile | null;
        if (!t) return null;
        const { x, y, z } = props.tile.index;
        const s = 2 ** -z;
        const x0 = x * TILE * s, y0 = y * TILE * s;
        return new LabelBitmapLayer({
          id: `${props.id}-labels`,
          labels: t,
          source: this,
          paletteVersion: this.paletteVersion,
          outline: style.outline,
          fillAlpha,
          bounds: [x0, y0 + t.height * s, x0 + t.width * s, y0],
          // sub-layers do not inherit the tile layer's opacity
          opacity: style.opacity,
          pickable: false,
        });
      },
    });
  }
}

// -- the GPU layer --------------------------------------------------------

const labelUniforms = {
  name: "labels",
  vs: "",
  fs: `\
layout(std140) uniform labelsUniforms {
  vec2 tileSize;
  float outline;
  float fillAlpha;
  float othersAlpha;
} labels;
`,
  uniformTypes: { tileSize: "vec2<f32>", outline: "f32", fillAlpha: "f32", othersAlpha: "f32" },
} as const;

const fs = `\
#version 300 es
#define SHADER_NAME label-layer-fragment-shader
precision highp float;
precision highp int;

uniform sampler2D bitmapTexture;
uniform sampler2D paletteTexture;

in vec2 vTexCoord;
in vec2 vTexPos;
out vec4 fragColor;

uint valueAt(ivec2 p) {
  uvec4 b = uvec4(round(texelFetch(bitmapTexture, p, 0) * 255.0));
#if LABEL_BYTES == 1
  return b.r;
#elif LABEL_BYTES == 2
  return b.r | (b.g << 8);
#else
  return b.r | (b.g << 8) | (b.b << 16) | (b.a << 24);
#endif
}

vec4 colorOf(uint v) {
  if (v < 65536u) return texelFetch(paletteTexture, ivec2(int(v & 255u), int(v >> 8)), 0);
  // same hash as hashColor() in labels.ts
  uint h = v * 2654435761u;
  h ^= h >> 15;
  h *= 2246822519u;
  h ^= h >> 13;
  return vec4(vec3(64u + (h & 127u), 64u + ((h >> 8) & 127u), 64u + ((h >> 16) & 127u)) / 255.0, labels.othersAlpha);
}

void main(void) {
  ivec2 lim = ivec2(labels.tileSize) - 1;
  ivec2 p = clamp(ivec2(floor(vTexCoord * labels.tileSize)), ivec2(0), lim);
  uint v = valueAt(p);
  vec4 c = colorOf(v);
  if (labels.outline > 0.5) {
    bool edge = valueAt(clamp(p + ivec2(1, 0), ivec2(0), lim)) != v
             || valueAt(clamp(p - ivec2(1, 0), ivec2(0), lim)) != v
             || valueAt(clamp(p + ivec2(0, 1), ivec2(0), lim)) != v
             || valueAt(clamp(p - ivec2(0, 1), ivec2(0), lim)) != v;
    if (!edge) c.a *= labels.fillAlpha;
  }
  if (c.a == 0.0) discard;
  fragColor = vec4(c.rgb, c.a * layer.opacity);
  geometry.uv = vTexCoord;
  DECKGL_FILTER_COLOR(fragColor, geometry);
}
`;

type LabelLayerProps = BitmapLayerProps & {
  labels: LabelTile;
  source: LabelSource;
  paletteVersion: number;
  outline: boolean;
  fillAlpha: number;
};

class LabelBitmapLayer extends BitmapLayer<LabelLayerProps> {
  static layerName = "LabelBitmapLayer";
  static defaultProps: DefaultProps<LabelLayerProps> = {
    ...(BitmapLayer.defaultProps as DefaultProps<LabelLayerProps>),
    labels: { type: "object", value: null, compare: true } as never,
    source: { type: "object", value: null, compare: true } as never,
    paletteVersion: 0,
    outline: false,
    fillAlpha: 0.15,
  };
  declare state: BitmapLayer["state"] & { texture?: Texture };

  getShaders() {
    const sh = super.getShaders();
    return { ...sh, fs, modules: [...sh.modules, labelUniforms],
             defines: { ...(sh.defines ?? {}), LABEL_BYTES: (this.props as LabelLayerProps).labels.bytes } };
  }

  updateState(params: UpdateParameters<this>) {
    super.updateState(params);
    const { props, oldProps } = params;
    if (props.labels !== oldProps.labels) {
      this.state.texture?.destroy();
      const t = props.labels;
      // full 512 x 512 texture (rows stay 4-byte aligned); the tile sits top-left
      const tex = this.context.device.createTexture({
        format: FORMAT[t.bytes], width: TILE, height: TILE, mipLevels: 1,
        sampler: { minFilter: "nearest", magFilter: "nearest" },
      });
      const padded = new Uint8Array(TILE * TILE * t.bytes);
      for (let r = 0; r < t.height; r++)
        padded.set(t.data.subarray(r * t.width * t.bytes, (r + 1) * t.width * t.bytes), r * TILE * t.bytes);
      tex.writeData(padded);
      this.setState({ texture: tex });
    }
  }

  finalizeState(context: Parameters<BitmapLayer["finalizeState"]>[0]) {
    this.state.texture?.destroy();
    super.finalizeState(context);
  }

  draw() {
    const { model, bounds, texture } = this.state as typeof this.state & { bounds: number[] };
    if (!model || !texture) return;
    const p = this.props as LabelLayerProps;
    model.shaderInputs.setProps({
      bitmap: { bitmapTexture: texture, bounds, coordinateConversion: 0, desaturate: 0,
                tintColor: [1, 1, 1], transparentColor: [0, 0, 0, 0] },
      labels: { tileSize: [p.labels.width, p.labels.height], outline: p.outline ? 1 : 0,
                fillAlpha: p.fillAlpha, othersAlpha: p.source.others ? 1 : 0 },
    } as never);
    model.setBindings({ paletteTexture: p.source.paletteOn(this.context.device) });
    model.draw(this.context.renderPass);
  }
}
