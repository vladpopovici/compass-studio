// Units and scale bar arithmetic for the status bar.

/** Largest 1, 2 or 5 x 10^k not above `x`. */
export function niceFloor(x: number): number {
  const p = 10 ** Math.floor(Math.log10(x));
  const m = x / p;
  return (m >= 5 ? 5 : m >= 2 ? 2 : 1) * p;
}

export function formatLength(um: number): string {
  if (um >= 1000) return `${+(um / 1000).toPrecision(3)} mm`;
  if (um >= 1) return `${+um.toPrecision(3)} µm`;
  return `${+(um * 1000).toPrecision(3)} nm`;
}

/** Scale bar of a round length, at most `maxPx` screen px wide. */
export function scaleBar(mpp: number | null | undefined, zoom: number, maxPx = 120) {
  const pxPerL0 = 2 ** zoom;
  if (mpp) {
    const um = niceFloor((maxPx / pxPerL0) * mpp);
    return { px: (um / mpp) * pxPerL0, label: formatLength(um) };
  }
  const l0 = niceFloor(maxPx / pxPerL0);
  return { px: l0 * pxPerL0, label: `${l0} px` };
}

/** Apparent magnification: objective power at 1:1, scaled by zoom. */
export function magnification(objective: number | null | undefined, zoom: number): string | null {
  if (!objective) return null;
  const m = objective * 2 ** zoom;
  return `${m >= 10 ? m.toFixed(0) : m >= 1 ? m.toFixed(1) : m.toFixed(2)}×`;
}

export function zoomPercent(zoom: number): string {
  const p = 100 * 2 ** zoom;
  return `${p >= 10 ? p.toFixed(0) : p >= 1 ? p.toFixed(1) : p.toFixed(2)} %`;
}

/** Pyramid level the tile layer shows at this zoom. */
export function shownLevel(zoom: number, levels: number): number {
  return Math.min(levels - 1, Math.max(0, Math.floor(-zoom + 1e-9)));
}
