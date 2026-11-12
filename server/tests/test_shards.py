"""Fast-path shard reader agrees with zarr-python.

Synthetic arrays always run. A real image is checked too when
COMPASS_STUDIO_TEST_IMAGE points to a COMPASS ``.zarr`` folder.
"""

import os
from pathlib import Path

import numpy as np
import pytest
import zarr
from zarr.codecs import BloscCodec

from compass_studio.imaging.shards import open_level


def _chunk_slices(arr, chunk):
    return tuple(slice(i * c, min((i + 1) * c, n))
                 for i, c, n in zip(chunk, arr.layout.chunk_shape, arr.layout.shape))


def _make(path, shape, dtype, shard, chunk, fill):
    a = zarr.create_array(
        store=str(path), shape=shape, dtype=dtype, shards=shard, chunks=chunk,
        compressors=[BloscCodec(cname="zstd", clevel=5, shuffle="bitshuffle")],
        fill_value=fill, zarr_format=3)
    return a


@pytest.mark.parametrize("dtype", ["uint8", "uint16", "int32"])
def test_matches_zarr(tmp_path, dtype):
    shape, shard, chunk = (700, 1100, 3), (256, 256, 3), (64, 64, 3)
    a = _make(tmp_path / "a", shape, dtype, shard, chunk, fill=7)
    rng = np.random.default_rng(0)
    data = rng.integers(0, 200, size=shape).astype(dtype)
    data[:300, :300] = 7          # chunks equal to fill: zarr may skip them
    a[:, :500] = data[:, :500]    # right part never written: missing shards
    a[600:, 900:] = data[600:, 900:]

    fast = open_level(tmp_path / "a")
    assert fast is not None
    expect = a[:]
    for chunk_idx in np.ndindex(*fast.layout.grid):
        got = fast.read_chunk(chunk_idx)
        np.testing.assert_array_equal(got, expect[_chunk_slices(fast, chunk_idx)])


def test_unsupported_falls_back(tmp_path):
    zarr.create_array(store=str(tmp_path / "b"), shape=(10, 10), dtype="uint8",
                      chunks=(5, 5), zarr_format=3)
    assert open_level(tmp_path / "b") is None


def test_out_of_grid(tmp_path):
    _make(tmp_path / "c", (100, 100), "uint8", (64, 64), (32, 32), 0)
    with pytest.raises(IndexError):
        open_level(tmp_path / "c").read_chunk((4, 0))


@pytest.mark.skipif("COMPASS_STUDIO_TEST_IMAGE" not in os.environ,
                    reason="set COMPASS_STUDIO_TEST_IMAGE to a COMPASS .zarr folder")
def test_real_image_sample():
    root = Path(os.environ["COMPASS_STUDIO_TEST_IMAGE"])
    g = zarr.open_group(str(root), mode="r")
    rng = np.random.default_rng(1)
    for name in sorted(k for k in g.array_keys()):
        a = g[name]
        fast = open_level(root / name)
        assert fast is not None, f"level {name} not on the fast path"
        grid = fast.layout.grid
        picks = {tuple(int(rng.integers(0, n)) for n in grid) for _ in range(40)}
        picks.add(tuple(n - 1 for n in grid))  # edge chunk
        for c in picks:
            np.testing.assert_array_equal(fast.read_chunk(c), a[_chunk_slices(fast, c)])
