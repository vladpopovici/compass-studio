// bench=pan: a scripted zoom/pan flight over tissue, run three times:
//   cold     new tile URLs (nothing cached anywhere)
//   http     a fresh TileLayer on the same URLs (browser HTTP cache)
//   resident the same TileLayer again (tiles already in deck's cache)
// Records rAF frame intervals and deck render CPU time per phase, per-tile latency
// (request -> decoded bitmap -> first frame drawn after it), the server's
// read/encode times from Server-Timing, and per frame the share of on-screen
// tiles shown at the target level ("sharp").
// &tiles=<preset> picks the tile loading options (TILE_PRESETS, default tuned).
// &labels=<id> draws that label map over the image (&outline=1: outlines);
// its tiles are timed separately ("label_tiles").

import { Deck, OrthographicView, type OrthographicViewState } from "@deck.gl/core";
import { getBlob, postResults, type ImageInfo } from "../api";
import { environment, nextFrame, percentiles } from "./env";
import { fitZoom, makeTileLayer, TILE_PRESETS, TileStats } from "../viewer/tiles";
import { DEFAULT_STYLE, LabelSource } from "../viewer/labels";

interface TileHeader { isLoaded: boolean; isSelected: boolean; bbox: { left: number; top: number; right: number; bottom: number } }

/** Share of selected tiles intersecting the view that are loaded (1 = all sharp). */
function sharpness(layer: unknown, view: [number, number, number, number]): number | null {
  const ts = (layer as { state?: { tileset?: { selectedTiles?: TileHeader[] } } }).state?.tileset;
  const tiles = ts?.selectedTiles;
  if (!tiles?.length) return null;
  const [vx0, vy0, vx1, vy1] = view;
  let n = 0, ok = 0;
  for (const t of tiles) {
    const { left, right } = t.bbox, top = Math.min(t.bbox.top, t.bbox.bottom), bottom = Math.max(t.bbox.top, t.bbox.bottom);
    if (left < vx1 && right > vx0 && top < vy1 && bottom > vy0) { n++; if (t.isLoaded) ok++; }
  }
  return n ? ok / n : null;
}


interface Phase { name: string; seconds: number; from: [number, number, number]; to: [number, number, number] }

/** Bounding box of non-background pixels, from the coarsest level (level-0 px). */
async function tissueBox(info: ImageInfo): Promise<[number, number, number, number]> {
  if (info.kind !== "zarr") return [0, 0, info.width, info.height];
  const l = info.levels.length - 1;
  const bmp = await createImageBitmap(await getBlob(`images/${info.id}/tiles/${l}/0/0.png`));
  const c = new OffscreenCanvas(bmp.width, bmp.height).getContext("2d")!;
  c.drawImage(bmp, 0, 0);
  const d = c.getImageData(0, 0, bmp.width, bmp.height).data;
  let x0 = Infinity, y0 = Infinity, x1 = -1, y1 = -1;
  for (let y = 0; y < bmp.height; y++)
    for (let x = 0; x < bmp.width; x++) {
      const i = 4 * (y * bmp.width + x);
      if (d[i] + d[i + 1] + d[i + 2] < 3 * 220) {
        x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x); y1 = Math.max(y1, y);
      }
    }
  const s = 2 ** l;
  return x1 < 0 ? [0, 0, info.width, info.height] : [x0 * s, y0 * s, (x1 + 1) * s, (y1 + 1) * s];
}

function flight(info: ImageInfo, box: [number, number, number, number]): Phase[] {
  const [bx0, by0, bx1, by1] = box;
  const cx = (bx0 + bx1) / 2, cy = (by0 + by1) / 2, fit = fitZoom(info);
  const speed = 1500; // screen px per second
  const phases: Phase[] = [{ name: "zoom-in", seconds: 3, from: [cx, cy, fit], to: [cx, cy, 0] }];
  let x = cx, y = cy;
  const legs: [string, number, number, number, number][] = [
    ["pan-z0", 0, 4, 1, 0], ["pan-z-2", -2, 4, 0, 1], ["pan-z-4", -4, 3, -1, 0],
    ["pan-z+2", 2, 3, 0, -1], ["fling-z0", 0, 1.5, 1, 1], ["fling-z-1", -1, 1.5, -1, 0],
  ];
  for (const [name, z, sec, dx, dy] of legs) {
    const d = ((name.startsWith("fling") ? 4 * speed : speed) * sec) / 2 ** z;
    const nx = Math.min(bx1, Math.max(bx0, x + dx * d)), ny = Math.min(by1, Math.max(by0, y + dy * d));
    phases.push({ name: `jump-${name}`, seconds: 0, from: [x, y, z], to: [x, y, z] });
    phases.push({ name, seconds: sec, from: [x, y, z], to: [nx, ny, z] });
    x = nx; y = ny;
  }
  phases.push({ name: "zoom-out", seconds: 3, from: [x, y, 2], to: [cx, cy, fit] });
  return phases;
}

export async function runPan(info: ImageInfo, hud: HTMLElement, presetName = "tuned",
                             labels: ImageInfo | null = null, outline = false) {
  const preset = TILE_PRESETS[presetName];
  if (!preset) throw new Error(`no tile preset ${presetName}`);
  let stats = new TileStats();
  let labelStats = new TileStats();
  const style = { ...DEFAULT_STYLE, outline };
  const vs = (x: number, y: number, z: number): OrthographicViewState =>
    ({ target: [x, y, 0], zoom: z });
  let gl: WebGL2RenderingContext | null = null;
  let renderStart = 0;
  let renderMs: number[] = [];
  const deck = new Deck({
    views: new OrthographicView({ id: "ortho", flipY: true }),
    viewState: vs(info.width / 2, info.height / 2, fitZoom(info)),
    controller: false,
    onBeforeRender: () => { renderStart = performance.now(); },
    onAfterRender: ({ gl: g }) => {
      gl = g;
      const now = performance.now();
      renderMs.push(now - renderStart);
      stats.markDrawn(now);
      labelStats.markDrawn(now);
    },
  });
  const box = await tissueBox(info);
  const phases = flight(info, box);
  const runs: Record<string, unknown>[] = [];
  const bust = `${Date.now()}`;

  for (const run of ["cold", "http", "resident"]) {
    stats = new TileStats();
    labelStats = new TileStats();
    if (run !== "resident") {
      const layer = makeTileLayer(info, stats, bust, preset);
      const layers = [layer.clone({ id: `${layer.id}-${run}` })];
      if (labels) {
        const src = new LabelSource(labels, preset, labelStats, bust);
        src.setStyle(style);
        const l = src.layer(style);
        layers.push(l.clone({ id: `${l.id}-${run}` }) as never);
      }
      deck.setProps({ layers });
    } else {
      deck.setProps({ layers: [...deck.props.layers] });
    }
    const phaseOut: Record<string, unknown>[] = [];
    for (const ph of phases) {
      hud.textContent = `bench=pan ${info.id} ${run} ${ph.name}`;
      const dts: number[] = [];
      const sharp: number[] = [];
      renderMs = [];
      const t0 = await nextFrame();
      let last = t0;
      for (;;) {
        const now = await nextFrame();
        const u = ph.seconds ? Math.min(1, (now - t0) / (ph.seconds * 1000)) : 1;
        const z = ph.from[2] + (ph.to[2] - ph.from[2]) * u;
        const cx = ph.from[0] + (ph.to[0] - ph.from[0]) * u, cy = ph.from[1] + (ph.to[1] - ph.from[1]) * u;
        deck.setProps({ viewState: vs(cx, cy, z) });
        const hw = innerWidth / 2 / 2 ** z, hh = innerHeight / 2 / 2 ** z;
        const sh = sharpness((deck.props.layers as unknown[])[0], [cx - hw, cy - hh, cx + hw, cy + hh]);
        if (sh !== null) sharp.push(sh);
        if (now > t0) dts.push(now - last);
        last = now;
        if (u >= 1) break;
      }
      if (ph.seconds) {
        phaseOut.push({
          phase: ph.name, frames: dts.length,
          frame_ms: percentiles(dts),
          render_cpu_ms: percentiles(renderMs),
          over_16_7ms: +(dts.filter((d) => d > 16.7 * 1.05).length / dts.length).toFixed(3),
          over_33_3ms: +(dts.filter((d) => d > 33.3 * 1.05).length / dts.length).toFixed(3),
          sharp_mean: sharp.length ? +(sharp.reduce((a, b) => a + b, 0) / sharp.length).toFixed(3) : null,
          sharp_full_frames: sharp.length ? +(sharp.filter((x) => x >= 1).length / sharp.length).toFixed(3) : null,
        });
      }
    }
    // let outstanding tiles land before summarising
    const until = performance.now() + 3000;
    while (stats.inflight + labelStats.inflight > 0 && performance.now() < until) await nextFrame();
    runs.push({ run, phases: phaseOut, tiles: stats.summary(),
                ...(labels ? { label_tiles: labelStats.summary() } : {}) });
  }

  const result = { spike: "S2", bench: "pan", tiles: { preset: presetName, ...preset,
    uploadsPerFrame: Number.isFinite(preset.uploadsPerFrame) ? preset.uploadsPerFrame : null },
    labels: labels ? { id: labels.id, dtype: labels.dtype, outline } : null, image: { id: info.id, kind: info.kind,
    width: info.width, height: info.height, levels: info.levels.length }, tissue_box: box,
    environment: environment(gl), deck_version: (await import("@deck.gl/core")).VERSION, runs };
  const saved = await postResults("s2-pan", result);
  hud.textContent = `bench=pan done -> ${saved}\n` + JSON.stringify(runs.map((r) => (r as { tiles: unknown }).tiles), null, 1);
  (window as unknown as { __benchDone?: unknown }).__benchDone = result;
  deck.finalize();
}
