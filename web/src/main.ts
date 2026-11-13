// S2 harness: deck.gl OrthographicView + TileLayer over the tile server.
//
// URL: /?image=<id>[&bench=pan|jitter]#token=<token>
//   no bench   interactive viewer with a HUD
//   bench=pan  scripted zoom/pan flight, cold then warm; posts frame and tile timings
//   bench=jitter  sub-pixel edge placement at large coordinates and deep zoom
//   bench=annot   S3: 5M points and 1M polygons resident (see annot.ts)
//   bench=edit    S5: check-out, vertex editing, commit (see edit.ts)

import { Deck, OrthographicView, type OrthographicViewState } from "@deck.gl/core";
import { getJSON, type ImageInfo } from "./api";
import { runAnnot } from "./annot";
import { runEdit } from "./edit";
import { runJitter } from "./jitter";
import { runPan } from "./pan";
import { fitZoom, makeTileLayer, TileStats } from "./tiles";

const params = new URLSearchParams(location.search);
const hud = document.getElementById("hud")!;

async function interactive(info: ImageInfo) {
  const stats = new TileStats();
  let viewState: OrthographicViewState = {
    target: [info.width / 2, info.height / 2, 0], zoom: fitZoom(info),
    minZoom: fitZoom(info) - 1, maxZoom: 8,
  };
  let cursor = "";
  const deck = new Deck({
    views: new OrthographicView({ id: "ortho", flipY: true }),
    viewState,
    controller: { inertia: 300, scrollZoom: { speed: 0.02, smooth: true } },
    onViewStateChange: ({ viewState: v }) => {
      viewState = v as OrthographicViewState;
      deck.setProps({ viewState });
    },
    onHover: ({ coordinate }) => {
      if (!coordinate) return;
      const [x, y] = coordinate;
      const um = info.mpp ? `  ${(x * info.mpp).toFixed(1)}, ${(y * info.mpp).toFixed(1)} µm` : "";
      cursor = `${x.toFixed(1)}, ${y.toFixed(1)} px${um}`;
    },
    layers: [makeTileLayer(info, stats)],
  });
  let frames = 0, fps = 0, t0 = performance.now();
  const tick = () => { frames++; requestAnimationFrame(tick); };
  requestAnimationFrame(tick);
  setInterval(() => {
    const now = performance.now();
    fps = (frames * 1000) / (now - t0); frames = 0; t0 = now;
    hud.textContent =
      `${info.id}  ${info.width}x${info.height}  ${info.levels.length} levels\n` +
      `fps ${fps.toFixed(0)}  zoom ${(viewState.zoom as number).toFixed(2)}\n` +
      `${cursor}\n${stats.summaryLine()}`;
  }, 250);
}

async function main() {
  const bench = params.get("bench");
  if (bench === "jitter") {
    await runJitter(hud);
    return;
  }
  if (bench === "annot") {
    await runAnnot(hud);
    return;
  }
  if (bench === "edit") {
    await runEdit(hud);
    return;
  }
  const images = await getJSON<ImageInfo[]>("images");
  const id = params.get("image") ?? images[0].id;
  const info = images.find((i) => i.id === id);
  if (!info) throw new Error(`no image ${id}`);
  if (bench === "pan") await runPan(info, hud);
  else await interactive(info);
}

main().catch((e) => {
  hud.textContent = `error: ${e}`;
  console.error(e);
  (window as unknown as { __benchDone?: unknown }).__benchDone = { error: String(e) };
});
