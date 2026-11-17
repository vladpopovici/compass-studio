// GeoJSON geometry helpers for editing (level-0 px): clean-up of drawn
// outlines, ellipses from boxes, validity checks matching the server's
// (shapely `is_valid`) for the common cases, and outlines for selection.

export type Pos = [number, number];
export type Geometry =
  | { type: "Point"; coordinates: Pos }
  | { type: "LineString"; coordinates: Pos[] }
  | { type: "Polygon"; coordinates: Pos[][] };

export type GeometryKind = "point" | "polyline" | "polygon";

export function kindOf(g: Geometry): GeometryKind {
  return g.type === "Point" ? "point" : g.type === "LineString" ? "polyline" : "polygon";
}

/** Drop consecutive duplicates (and, for rings, keep it closed). */
function dedupe(pts: Pos[], closed: boolean): Pos[] {
  const out: Pos[] = [];
  for (const p of pts) {
    const q = out[out.length - 1];
    if (!q || q[0] !== p[0] || q[1] !== p[1]) out.push([p[0], p[1]]);
  }
  if (closed) {
    if (out.length > 1 && out[0][0] === out[out.length - 1][0] && out[0][1] === out[out.length - 1][1]) out.pop();
    if (out.length) out.push([out[0][0], out[0][1]]);
  }
  return out;
}

/** Douglas-Peucker simplification with tolerance `tol` (open polyline; endpoints kept). */
export function simplify(pts: Pos[], tol: number): Pos[] {
  if (pts.length < 3) return pts.slice();
  const keep = new Uint8Array(pts.length);
  keep[0] = keep[pts.length - 1] = 1;
  const stack: [number, number][] = [[0, pts.length - 1]];
  const t2 = tol * tol;
  while (stack.length) {
    const [a, b] = stack.pop()!;
    const [ax, ay] = pts[a], [bx, by] = pts[b];
    const dx = bx - ax, dy = by - ay, len2 = dx * dx + dy * dy;
    let best = -1, bestD = t2;
    for (let i = a + 1; i < b; i++) {
      const [px, py] = pts[i];
      let t = len2 ? ((px - ax) * dx + (py - ay) * dy) / len2 : 0;
      t = Math.max(0, Math.min(1, t));
      const ex = ax + t * dx - px, ey = ay + t * dy - py, d = ex * ex + ey * ey;
      if (d > bestD) { bestD = d; best = i; }
    }
    if (best >= 0) {
      keep[best] = 1;
      stack.push([a, best], [best, b]);
    }
  }
  return pts.filter((_, i) => keep[i]);
}

/** Simplify a closed ring (first point repeated at the end) with tolerance `tol`. */
export function simplifyRing(ring: Pos[], tol: number): Pos[] {
  const r = dedupe(ring, true);
  if (r.length <= 4) return r;
  // split at the point farthest from the start so both halves keep their shape
  let far = 1, farD = -1;
  for (let i = 1; i < r.length - 1; i++) {
    const d = (r[i][0] - r[0][0]) ** 2 + (r[i][1] - r[0][1]) ** 2;
    if (d > farD) { farD = d; far = i; }
  }
  const a = simplify(r.slice(0, far + 1), tol), b = simplify(r.slice(far), tol);
  return dedupe([...a, ...b.slice(1)], true);
}

/** An ellipse inscribed in the bounding box of `pts`, as a closed ring of `n` vertices. */
export function ellipseInBox(pts: Pos[], n = 64): Pos[] {
  const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
  const cx = (x0 + x1) / 2, cy = (y0 + y1) / 2, rx = (x1 - x0) / 2, ry = (y1 - y0) / 2;
  const ring: Pos[] = [];
  for (let i = 0; i < n; i++) {
    const t = (2 * Math.PI * i) / n;
    ring.push([cx + rx * Math.cos(t), cy + ry * Math.sin(t)]);
  }
  ring.push([ring[0][0], ring[0][1]]);
  return ring;
}

/** Normalise a geometry from the editor: plain numbers, no repeated vertices, closed rings. */
export function clean(g: Geometry): Geometry {
  if (g.type === "Point") return { type: "Point", coordinates: [g.coordinates[0], g.coordinates[1]] };
  if (g.type === "LineString") return { type: "LineString", coordinates: dedupe(g.coordinates, false) };
  return { type: "Polygon", coordinates: g.coordinates.map((r) => dedupe(r, true)) };
}

function orient(a: Pos, b: Pos, c: Pos): number {
  const v = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]);
  return v > 0 ? 1 : v < 0 ? -1 : 0;
}

function onSegment(a: Pos, b: Pos, p: Pos): boolean {
  return Math.min(a[0], b[0]) <= p[0] && p[0] <= Math.max(a[0], b[0]) &&
         Math.min(a[1], b[1]) <= p[1] && p[1] <= Math.max(a[1], b[1]);
}

function segmentsMeet(a: Pos, b: Pos, c: Pos, d: Pos): boolean {
  const o1 = orient(a, b, c), o2 = orient(a, b, d), o3 = orient(c, d, a), o4 = orient(c, d, b);
  if (o1 !== o2 && o3 !== o4) return true;
  return (o1 === 0 && onSegment(a, b, c)) || (o2 === 0 && onSegment(a, b, d)) ||
         (o3 === 0 && onSegment(c, d, a)) || (o4 === 0 && onSegment(c, d, b));
}

/** Whether a path crosses or touches itself (adjacent segments may share their vertex). */
function selfIntersects(pts: Pos[], closed: boolean): boolean {
  const n = pts.length - 1; // segments
  for (let i = 0; i < n; i++) {
    const a = pts[i], b = pts[i + 1];
    const ax0 = Math.min(a[0], b[0]), ax1 = Math.max(a[0], b[0]), ay0 = Math.min(a[1], b[1]), ay1 = Math.max(a[1], b[1]);
    for (let j = i + 2; j < n; j++) {
      if (closed && i === 0 && j === n - 1) continue; // first and last segments share the closing vertex
      const c = pts[j], d = pts[j + 1];
      if (Math.max(c[0], d[0]) < ax0 || Math.min(c[0], d[0]) > ax1 ||
          Math.max(c[1], d[1]) < ay0 || Math.min(c[1], d[1]) > ay1) continue;
      if (segmentsMeet(a, b, c, d)) return true;
    }
  }
  return false;
}

function ringArea(r: Pos[]): number {
  let s = 0;
  for (let i = 0; i < r.length - 1; i++) s += r[i][0] * r[i + 1][1] - r[i + 1][0] * r[i][1];
  return Math.abs(s) / 2;
}

/** Above this many vertices per ring the O(n^2) check is left to the server. */
const CHECK_LIMIT = 4000;

/** Why a geometry would be refused, or null if it looks valid. */
export function problem(g: Geometry): string | null {
  if (g.type === "Point") return Number.isFinite(g.coordinates[0]) && Number.isFinite(g.coordinates[1]) ? null : "bad point";
  if (g.type === "LineString") {
    if (g.coordinates.length < 2) return "a line needs at least 2 points";
    return null;
  }
  for (const [i, r] of g.coordinates.entries()) {
    if (r.length < 4) return i ? "a hole needs at least 3 points" : "a polygon needs at least 3 points";
    if (ringArea(r) === 0) return "the outline has no area";
    if (r.length <= CHECK_LIMIT && selfIntersects(r, true)) return "the outline crosses itself";
  }
  return null;
}

/** Bounding-box centre. */
export function centreOf(g: Geometry): Pos {
  const pts = g.type === "Point" ? [g.coordinates] : g.type === "LineString" ? g.coordinates : g.coordinates[0];
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const [x, y] of pts) {
    x0 = Math.min(x0, x); x1 = Math.max(x1, x); y0 = Math.min(y0, y); y1 = Math.max(y1, y);
  }
  return [(x0 + x1) / 2, (y0 + y1) / 2];
}

/** Outline paths for the selection overlay. */
export function outlineOf(g: Geometry): { paths: Float32Array[]; closed: boolean } | undefined {
  if (g.type === "Point") return undefined;
  if (g.type === "LineString") return { paths: [Float32Array.from(g.coordinates.flat())], closed: false };
  return { paths: g.coordinates.map((r) => Float32Array.from(r.slice(0, -1).flat())), closed: true };
}
