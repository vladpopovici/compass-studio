"""A small in-memory cache of decoded tiles (milestone 8).

A multichannel tile is one Zarr chunk holding every channel, but the browser
asks for one channel per request; with six channels shown, six requests
arrive for the same chunk at about the same time. The cache keeps decoded
tiles (bounded by bytes) and lets concurrent requests for the same tile wait
for one read instead of reading it six times.
"""

import threading
from collections import OrderedDict
from collections.abc import Callable, Hashable

import numpy as np


class TileCache:
    def __init__(self, max_bytes: int = 256 << 20):
        self.max_bytes = max_bytes
        self.bytes = 0
        self.hits = self.misses = 0
        self._items: OrderedDict[Hashable, np.ndarray] = OrderedDict()
        self._loading: dict[Hashable, threading.Event] = {}
        self._lock = threading.Lock()

    def get(self, key: Hashable, load: Callable[[], np.ndarray]) -> np.ndarray:
        while True:
            with self._lock:
                arr = self._items.get(key)
                if arr is not None:
                    self._items.move_to_end(key)
                    self.hits += 1
                    return arr
                ev = self._loading.get(key)
                if ev is None:
                    ev = self._loading[key] = threading.Event()
                    self.misses += 1
                    break
            ev.wait()  # another thread is reading it; then look again
        try:
            arr = load()
            with self._lock:
                if arr.nbytes <= self.max_bytes:
                    self._items[key] = arr
                    self.bytes += arr.nbytes
                    while self.bytes > self.max_bytes:
                        _, old = self._items.popitem(last=False)
                        self.bytes -= old.nbytes
            return arr
        finally:
            with self._lock:
                del self._loading[key]
            ev.set()
