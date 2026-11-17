// Multichannel images (milestone 8, D4): one raw plane per channel and tile
// (contract 1.7 `bin` with `channel`), uploaded as textures and mixed on the
// GPU, so colour and contrast changes never refetch tiles.
//
// Each shown channel is its own TileLayer: turning a channel on fetches only
// that channel, and the browser caches each channel's tiles. Channels are
// added together over a black backdrop (additive blending, as a fluorescence
// microscope shows them); refinement is "no-overlap" so a parent tile and its
// children are never added twice while loading. uint16 values are packed into
// rg8 textures and decoded in the shader, like label maps.

import { BitmapLayer, SolidPolygonLayer, type BitmapLayerProps } from "@deck.gl/layers";
import { TileLayer } from "@deck.gl/geo-layers";
import type { DefaultProps, Layer, UpdateParameters } from "@deck.gl/core";
import type { Texture, TextureFormat } from "@luma.gl/core";
import type { ImageInfo } from "../api";
import { ringTileset, TILE_PRESETS, type TileOptions, type TileStats } from "./tiles";

const TILE = 512;
/** channels mixed at once */
export const MAX_SHOWN = 8;
const FORMAT: Record<1 | 2, TextureFormat> = { 1: "r8unorm", 2: "rg8unorm" };

export interface ChannelSetting {
  visible: boolean;
  /** #rrggbb */
  color: string;
  /** value shown black */
  min: number;
  /** value shown at full colour */
  max: number;
}

interface ChannelTile { data: Uint8Array; width: number; height: number; bytes: 1 | 2 }

function rgb01(c: string): [number, number, number] {
  const n = parseInt(c.slice(1), 16);
  return [((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255];
}

/** One multichannel image: its loaded tiles (for values under the cursor) and its layers. */
export class ChannelSource {
  readonly bytes: 1 | 2;
  readonly maxValue: number;
  private tiles = new Map<string, ChannelTile>();

  constructor(readonly info: ImageInfo, private stats: TileStats, private options: TileOptions = TILE_PRESETS.tuned) {
    if (info.dtype !== "uint8" && info.dtype !== "uint16") throw new Error(`channel dtype ${info.dtype} not supported`);
    this.bytes = info.dtype === "uint8" ? 1 : 2;
    this.maxValue = info.dtype === "uint8" ? 255 : 65535;
  }

  /** Values of the shown channels at a level-0 point, from the finest loaded tile at or above `level`. */
  valuesAt(x: number, y: number, level: number, settings: ChannelSetting[]): { channel: number; value: number }[] {
    const out: { channel: number; value: number }[] = [];
    if (x < 0 || y < 0 || x >= this.info.width || y >= this.info.height) return out;
    settings.forEach((s, c) => {
      if (!s.visible) return;
      for (let l = Math.max(0, level); l < this.info.levels.length; l++) {
        const px = Math.floor(x / 2 ** l), py = Math.floor(y / 2 ** l);
        const t = this.tiles.get(`${c}/${l}/${Math.floor(px / TILE)}/${Math.floor(py / TILE)}`);
        if (!t) continue;
        const i = (py % TILE) * t.width + (px % TILE);
        out.push({ channel: c, value: t.bytes === 1 ? t.data[i] : t.data[2 * i] | (t.data[2 * i + 1] << 8) });
        return;
      }
    });
    return out;
  }

  destroy() {
    this.tiles.clear();
  }

  layers(settings: ChannelSetting[]): Layer[] {
    const info = this.info;
    const out: Layer[] = [new SolidPolygonLayer({
      id: `channels-${info.id}-backdrop`,
      data: [[[0, 0], [info.width, 0], [info.width, info.height], [0, info.height]]],
      getPolygon: (d: number[][]) => d as never,
      getFillColor: [0, 0, 0, 255],
    })];
    settings.forEach((s, c) => {
      if (s.visible) out.push(this.channelLayer(c, s));
    });
    return out;
  }

  private async fetchTile(c: number, level: number, col: number, row: number, signal?: AbortSignal): Promise<ChannelTile> {
    const v = this.info.version ? `&v=${encodeURIComponent(this.info.version)}` : "";
    const data = await this.stats.fetchBytes(`images/${this.info.id}/tiles/${level}/${col}/${row}.bin?channel=${c}${v}`, signal);
    const L = this.info.levels[level];
    const width = Math.min(TILE, L.width - col * TILE), height = Math.min(TILE, L.height - row * TILE);
    if (data.length !== width * height * this.bytes) throw new Error("channel tile: unexpected size");
    return { data, width, height, bytes: this.bytes };
  }

  private channelLayer(c: number, s: ChannelSetting) {
    const info = this.info, n = info.levels.length;
    const level = (z: number) => info.levels[Math.min(n - 1, Math.max(0, -z))];
    const color = rgb01(s.color);
    return new TileLayer<ChannelTile>({
      id: `channels-${info.id}-${c}`,
      tileSize: TILE,
      extent: [0, 0, info.width, info.height],
      minZoom: -(n - 1),
      maxZoom: 0,
      refinementStrategy: "no-overlap",
      maxRequests: this.options.maxRequests,
      TilesetClass: ringTileset(this.options.prefetchRing,
                                (z) => Math.ceil(level(z).width / TILE), (z) => Math.ceil(level(z).height / TILE)),
      loadOptions: { worker: false },
      getTileData: async ({ index: { x, y, z }, signal }) => {
        const t = await this.fetchTile(c, -z, x, y, signal);
        this.tiles.set(`${c}/${-z}/${x}/${y}`, t);
        return t;
      },
      onTileUnload: (tile) => {
        const { x, y, z } = tile.index;
        this.tiles.delete(`${c}/${-z}/${x}/${y}`);
      },
      updateTriggers: { renderSubLayers: [s.color, s.min, s.max] },
      renderSubLayers: (props) => {
        const t = props.data as ChannelTile | null;
        if (!t) return null;
        const { x, y, z } = props.tile.index;
        const sc = 2 ** -z;
        const x0 = x * TILE * sc, y0 = y * TILE * sc;
        return new ChannelBitmapLayer({
          id: `${props.id}-ch`,
          plane: t,
          lo: s.min,
          hi: Math.max(s.max, s.min + 1),
          color,
          bounds: [x0, y0 + t.height * sc, x0 + t.width * sc, y0],
          pickable: false,
          // add to what is below (the backdrop and the other channels)
          parameters: {
            blend: true, blendColorOperation: "add", blendColorSrcFactor: "one", blendColorDstFactor: "one",
            blendAlphaOperation: "add", blendAlphaSrcFactor: "one", blendAlphaDstFactor: "one",
          },
        });
      },
    });
  }
}

// -- the GPU layer --------------------------------------------------------

const channelUniforms = {
  name: "channel",
  vs: "",
  fs: `\
layout(std140) uniform channelUniforms {
  vec2 tileSize;
  float lo;
  float hi;
  vec3 color;
} channel;
`,
  uniformTypes: { tileSize: "vec2<f32>", lo: "f32", hi: "f32", color: "vec3<f32>" },
} as const;

const fs = `\
#version 300 es
#define SHADER_NAME channel-layer-fragment-shader
precision highp float;

uniform sampler2D bitmapTexture;

in vec2 vTexCoord;
in vec2 vTexPos;
out vec4 fragColor;

void main(void) {
  ivec2 p = clamp(ivec2(floor(vTexCoord * channel.tileSize)), ivec2(0), ivec2(channel.tileSize) - 1);
  vec4 b = round(texelFetch(bitmapTexture, p, 0) * 255.0);
#if CHANNEL_BYTES == 1
  float v = b.r;
#else
  float v = b.r + b.g * 256.0;
#endif
  float t = clamp((v - channel.lo) / (channel.hi - channel.lo), 0.0, 1.0);
  fragColor = vec4(channel.color * t, 1.0);
  geometry.uv = vTexCoord;
  DECKGL_FILTER_COLOR(fragColor, geometry);
}
`;

type ChannelLayerProps = BitmapLayerProps & { plane: ChannelTile; lo: number; hi: number; color: [number, number, number] };

class ChannelBitmapLayer extends BitmapLayer<ChannelLayerProps> {
  static layerName = "ChannelBitmapLayer";
  static defaultProps: DefaultProps<ChannelLayerProps> = {
    ...(BitmapLayer.defaultProps as DefaultProps<ChannelLayerProps>),
    plane: { type: "object", value: null, compare: true } as never,
    lo: 0,
    hi: 1,
    color: { type: "array", value: [1, 1, 1], compare: true } as never,
  };
  declare state: BitmapLayer["state"] & { texture?: Texture };

  getShaders() {
    const sh = super.getShaders();
    return { ...sh, fs, modules: [...sh.modules, channelUniforms],
             defines: { ...(sh.defines ?? {}), CHANNEL_BYTES: (this.props as ChannelLayerProps).plane.bytes } };
  }

  updateState(params: UpdateParameters<this>) {
    super.updateState(params);
    const { props, oldProps } = params;
    if (props.plane !== oldProps.plane) {
      this.state.texture?.destroy();
      const t = props.plane;
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
    const p = this.props as ChannelLayerProps;
    model.shaderInputs.setProps({
      bitmap: { bitmapTexture: texture, bounds, coordinateConversion: 0, desaturate: 0,
                tintColor: [1, 1, 1], transparentColor: [0, 0, 0, 0] },
      channel: { tileSize: [p.plane.width, p.plane.height], lo: p.lo, hi: p.hi, color: p.color },
    } as never);
    model.draw(this.context.renderPass);
  }
}
