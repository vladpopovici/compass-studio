// bench=jitter: does a shape edge land where it should at large level-0
// coordinates and deep zoom (D5 [verify], float32 precision)?
//
// For each world x0 and zoom, a white shape whose left edge is at x0 is
// drawn on black; the view target moves in sub-pixel steps, and the edge is
// located on a read-back row. With exact arithmetic the edge's screen
// column is ceil(W/2 + (x0 - target) * 2^zoom - 0.5). x0 has a fractional
// part (+0.3) so float32 rounding of positions is visible. The error in
// device pixels is reported per variant:
//   polygon-abs   accessor data, absolute coords (deck keeps fp64 positions)
//   bitmap-abs    BitmapLayer bounds, absolute coords
//   polygon-f32   binary Float32Array positions, absolute coords (as in S3)
//   polygon-f32-model  binary Float32Array relative to x0, plus a modelMatrix
//                      translation by x0 (precision-preserving candidate)
// coordinateOrigin is not tested: with an OrthographicView (identity
// projection) deck.gl ignores it for CARTESIAN coordinates (observed in the
// first run, 2026-11-12: shapes rendered off-screen).
// Antialiasing and device-pixel scaling are off so columns are exact. The
// canvas clears to transparent black, so only shape pixels have red > 127.

import { Deck, OrthographicView, type Layer } from "@deck.gl/core";
import { BitmapLayer, SolidPolygonLayer } from "@deck.gl/layers";
import { postResults } from "./api";
import { environment, nextFrame } from "./env";

const XS = [1_000, 50_000, 100_000, 150_000, 199_000, 262_000].map((x) => x + 0.3);
const ZOOMS = [0, 2, 4, 6, 7, 8];
const SUB = 16;      // target steps per world pixel
const SIZE = 4000;   // shape extent, world px
const VARIANTS = ["polygon-abs", "bitmap-abs", "polygon-f32", "polygon-f32-model"];

function white(): ImageData {
  const d = new ImageData(16, 16);
  d.data.fill(255);
  return d;
}

function rect(x0: number, y0: number): number[] {
  // closed ring; counter-clockwise on screen with y down
  return [x0, y0 - SIZE, x0, y0 + SIZE, x0 + SIZE, y0 + SIZE, x0 + SIZE, y0 - SIZE, x0, y0 - SIZE];
}

function layer(variant: string, x0: number, y0: number, img: ImageData): Layer {
  const id = `${variant}-${x0}`;
  switch (variant) {
    case "polygon-abs": {
      const r = rect(x0, y0);
      const ring: [number, number][] = [];
      for (let i = 0; i < r.length; i += 2) ring.push([r[i], r[i + 1]]);
      return new SolidPolygonLayer({ id, data: [ring], getPolygon: (d: [number, number][]) => d,
                                     getFillColor: [255, 255, 255] });
    }
    case "bitmap-abs":
      return new BitmapLayer({ id, image: img, bounds: [x0, y0 + SIZE, x0 + SIZE, y0 - SIZE],
                               textureParameters: { minFilter: "nearest", magFilter: "nearest" } });
    case "polygon-f32":
    case "polygon-f32-model": {
      const model = variant === "polygon-f32-model";
      const value = new Float32Array(model ? rect(0, 0) : rect(x0, y0));
      return new SolidPolygonLayer({
        id, _normalize: false, positionFormat: "XY",
        data: { length: 1, startIndices: new Uint32Array([0, 5]),
                attributes: { getPolygon: { value, size: 2 } } },
        getFillColor: [255, 255, 255],
        ...(model ? { modelMatrix: [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, x0, y0, 0, 1] } : {}),
      } as never);
    }
  }
  throw new Error(variant);
}

export async function runJitter(hud: HTMLElement) {
  let gl: WebGL2RenderingContext | null = null;
  let pending: ((row: Uint8Array) => void) | null = null;
  const W = innerWidth, H = innerHeight;
  const deck = new Deck({
    views: new OrthographicView({ id: "ortho", flipY: true }),
    viewState: { target: [0, 0, 0], zoom: 0 },
    controller: false,
    useDevicePixels: false,
    deviceProps: { type: "webgl", webgl: { antialias: false, preserveDrawingBuffer: true } },
    onAfterRender: ({ gl: g }) => {
      gl = g;
      if (!pending) return;
      const row = new Uint8Array(W * 4);
      g.readPixels(0, Math.floor(H / 2), W, 1, g.RGBA, g.UNSIGNED_BYTE, row);
      const p = pending;
      pending = null;
      p(row);
    },
  });
  const img = white();
  const variants = VARIANTS;
  const out: Record<string, unknown>[] = [];

  for (const variant of variants) {
    for (const x0 of XS) {
      const y0 = x0; // also stresses y; only x is measured
      deck.setProps({ layers: [layer(variant, x0, y0, img)] });
      for (let k = 0; k < 3; k++) await nextFrame(); // texture upload
      for (const z of ZOOMS) {
        hud.textContent = `bench=jitter ${variant} x0=${x0} zoom=${z}`;
        const errs: number[] = [];
        let steps = 0, missing = 0;
        for (let s = 0; s < SUB; s++) {
          const target = x0 + s / SUB;
          const row = await new Promise<Uint8Array>((resolve) => {
            pending = resolve;
            deck.setProps({ viewState: { target: [target, y0, 0], zoom: z } });
            deck.redraw("jitter");
          });
          let col = -1;
          for (let i = 0; i < W; i++) if (row[4 * i] > 127) { col = i; break; }
          const expect = Math.ceil(W / 2 + (x0 - target) * 2 ** z - 0.5);
          if (col < 0) missing++;
          else errs.push(col - expect);
          steps++;
        }
        const maxAbs = errs.length ? Math.max(...errs.map(Math.abs)) : null;
        out.push({ variant, x0, zoom: z, steps, missing, max_abs_err_px: maxAbs,
                   errors: errs });
      }
    }
  }

  const worst = (v: string) => Math.max(...out.filter((o) => o.variant === v)
    .map((o) => (o.max_abs_err_px as number | null) ?? Infinity));
  const summary = Object.fromEntries(variants.map((v) => [v, worst(v)]));
  const result = { spike: "S2", bench: "jitter", viewport: [W, H], xs: XS, zooms: ZOOMS,
    sub_steps: SUB, summary_max_abs_err_px: summary, environment: environment(gl),
    deck_version: (await import("@deck.gl/core")).VERSION, cases: out };
  const saved = await postResults("s2-jitter", result);
  hud.textContent = `bench=jitter done -> ${saved}\n${JSON.stringify(summary, null, 1)}`;
  (window as unknown as { __benchDone?: unknown }).__benchDone = result;
  deck.finalize();
}
