"""Direct reader for sharded Zarr v3 directory stores (D3 fast path).

Reads one inner chunk per call: the shard index is parsed once and cached,
then the chunk bytes are fetched with a single ``os.pread`` and decoded.
Only the layout COMPASS writes is handled (regular grid, default key
encoding, ``sharding_indexed`` with ``bytes`` + ``blosc``/``zstd`` inner
codecs and a ``bytes`` + ``crc32c`` index); :func:`open_level` returns
``None`` for anything else so callers fall back to zarr-python.
"""

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path

import crc32c
import numcodecs
import numpy as np

_EMPTY = np.uint64(2**64 - 1)


class UnsupportedLayout(ValueError):
    """The array uses a layout or codec the fast path does not handle."""


@dataclass(frozen=True)
class Layout:
    shape: tuple[int, ...]
    dtype: np.dtype
    fill_value: object
    shard_shape: tuple[int, ...]
    chunk_shape: tuple[int, ...]
    separator: str
    index_at_end: bool
    inner_endian: str | None
    inner_compressor: object | None

    @property
    def chunks_per_shard(self) -> tuple[int, ...]:
        return tuple(s // c for s, c in zip(self.shard_shape, self.chunk_shape))

    @property
    def grid(self) -> tuple[int, ...]:
        """Number of inner chunks along each axis."""
        return tuple(-(-n // c) for n, c in zip(self.shape, self.chunk_shape))


def parse_layout(meta: dict) -> Layout:
    """Array ``zarr.json`` -> :class:`Layout`; raises UnsupportedLayout."""
    if meta.get("zarr_format") != 3 or meta.get("node_type") != "array":
        raise UnsupportedLayout("not a Zarr v3 array")
    if meta.get("storage_transformers"):
        raise UnsupportedLayout("storage transformers")
    grid = meta["chunk_grid"]
    if grid["name"] != "regular":
        raise UnsupportedLayout(f"chunk grid {grid['name']}")
    cke = meta["chunk_key_encoding"]
    if cke["name"] != "default":
        raise UnsupportedLayout(f"chunk key encoding {cke['name']}")
    codecs = meta["codecs"]
    if len(codecs) != 1 or codecs[0]["name"] != "sharding_indexed":
        raise UnsupportedLayout("not sharded")
    sc = codecs[0]["configuration"]

    idx = sc.get("index_codecs", [])
    if ([c["name"] for c in idx] != ["bytes", "crc32c"]
            or idx[0].get("configuration", {}).get("endian", "little") != "little"):
        raise UnsupportedLayout("shard index codecs")

    endian, compressor = None, None
    inner = sc["codecs"]
    if not inner or inner[0]["name"] != "bytes":
        raise UnsupportedLayout("inner codecs must start with bytes")
    endian = inner[0].get("configuration", {}).get("endian")
    for c in inner[1:]:
        if compressor is not None:
            raise UnsupportedLayout("more than one inner compressor")
        if c["name"] == "blosc":
            compressor = numcodecs.Blosc()  # parameters live in the blosc header
        elif c["name"] == "zstd":
            compressor = numcodecs.Zstd()
        else:
            raise UnsupportedLayout(f"inner codec {c['name']}")

    dtype = np.dtype(meta["data_type"])
    if dtype.itemsize > 1:
        dtype = dtype.newbyteorder("<" if endian != "big" else ">")
    shard_shape = tuple(grid["configuration"]["chunk_shape"])
    chunk_shape = tuple(sc["chunk_shape"])
    if any(s % c for s, c in zip(shard_shape, chunk_shape)):
        raise UnsupportedLayout("shard shape not a multiple of chunk shape")
    return Layout(
        shape=tuple(meta["shape"]), dtype=dtype, fill_value=meta["fill_value"],
        shard_shape=shard_shape, chunk_shape=chunk_shape,
        separator=cke.get("configuration", {}).get("separator", "/"),
        index_at_end=sc.get("index_location", "end") == "end",
        inner_endian=endian, inner_compressor=compressor)


class ShardedArray:
    """Read-only access to one sharded array, by inner chunk."""

    def __init__(self, path: str | Path, layout: Layout):
        self.path = Path(path)
        self.layout = layout
        n = int(np.prod(layout.chunks_per_shard))
        self._index_nbytes = 16 * n + 4
        self._index: dict[tuple[int, ...], np.ndarray | None] = {}
        self._fds: dict[tuple[int, ...], int] = {}
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            for fd in self._fds.values():
                os.close(fd)
            self._fds.clear()
            self._index.clear()

    def _shard_file(self, shard: tuple[int, ...]) -> Path:
        key = self.layout.separator.join(["c", *map(str, shard)])
        return self.path.joinpath(*key.split("/"))

    def _shard(self, shard: tuple[int, ...]) -> tuple[int, np.ndarray] | None:
        """(fd, index of shape (*chunks_per_shard, 2)) or None if absent."""
        if shard in self._index:
            index = self._index[shard]
            return None if index is None else (self._fds[shard], index)
        with self._lock:
            if shard in self._index:
                index = self._index[shard]
                return None if index is None else (self._fds[shard], index)
            try:
                fd = os.open(self._shard_file(shard), os.O_RDONLY)
            except FileNotFoundError:
                self._index[shard] = None
                return None
            size = os.fstat(fd).st_size
            off = size - self._index_nbytes if self.layout.index_at_end else 0
            raw = os.pread(fd, self._index_nbytes, off)
            body, crc = raw[:-4], int.from_bytes(raw[-4:], "little")
            if len(raw) != self._index_nbytes or crc32c.crc32c(body) != crc:
                os.close(fd)
                raise OSError(f"corrupt shard index: {self._shard_file(shard)}")
            index = np.frombuffer(body, dtype="<u8").reshape(
                (*self.layout.chunks_per_shard, 2))
            self._fds[shard] = fd
            self._index[shard] = index
            return fd, index

    def read_chunk(self, chunk: tuple[int, ...]) -> np.ndarray:
        """Inner chunk at grid position ``chunk``, cropped at array edges."""
        L = self.layout
        if len(chunk) != len(L.shape) or any(not 0 <= c < g for c, g in zip(chunk, L.grid)):
            raise IndexError(f"chunk {chunk} outside grid {L.grid}")
        cps = L.chunks_per_shard
        shard = tuple(c // k for c, k in zip(chunk, cps))
        within = tuple(c % k for c, k in zip(chunk, cps))
        valid = tuple(min(c, n - i * c) for i, c, n in zip(chunk, L.chunk_shape, L.shape))

        got = self._shard(shard)
        if got is not None:
            fd, index = got
            off, nbytes = index[within]
            if off != _EMPTY:
                raw = os.pread(fd, int(nbytes), int(off))
                if L.inner_compressor is not None:
                    raw = L.inner_compressor.decode(raw)
                arr = np.frombuffer(raw, dtype=L.dtype).reshape(L.chunk_shape)
                return arr[tuple(slice(0, v) for v in valid)]
        return np.full(valid, L.fill_value, dtype=L.dtype)


def open_level(path: str | Path) -> ShardedArray | None:
    """Open an array directory with the fast path, or None if unsupported."""
    path = Path(path)
    meta = json.loads((path / "zarr.json").read_text())
    try:
        return ShardedArray(path, parse_layout(meta))
    except UnsupportedLayout:
        return None
