"""Polygon triangulation (earcut) for GPU delivery (D6).

Input is a batch of polygons in the layout deck.gl's SolidPolygonLayer takes:
one flat array of x, y vertices and a start offset per polygon (in vertices).
Each polygon is one exterior ring, not closed, optionally followed by holes
(``hole_starts``, vertex offsets within the polygon). Output indices are
local to each polygon; callers add the polygon's vertex start to make them
global.
"""

import mapbox_earcut as earcut
import numpy as np


def triangulate(xy: np.ndarray, starts: np.ndarray,
                hole_starts: list[np.ndarray] | None = None) -> tuple[np.ndarray, np.ndarray]:
    """(n_vertices, 2) vertices and (n + 1,) polygon starts ->
    (local uint32 indices, (n + 1,) index offsets)."""
    xy = np.ascontiguousarray(xy, dtype=np.float64)
    n = len(starts) - 1
    parts: list[np.ndarray] = []
    offsets = np.zeros(n + 1, np.int64)
    for i in range(n):
        s, e = int(starts[i]), int(starts[i + 1])
        rings = [e - s] if hole_starts is None or not len(hole_starts[i]) \
            else [*hole_starts[i].tolist(), e - s]
        tri = earcut.triangulate_float64(xy[s:e], np.asarray(rings, np.uint32))
        parts.append(tri.astype(np.uint32, copy=False))
        offsets[i + 1] = offsets[i] + len(tri)
    idx = np.concatenate(parts) if parts else np.zeros(0, np.uint32)
    return idx, offsets
