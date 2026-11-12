# S1: tile throughput

zarr-python vs the direct shard reader (`server/src/compass_studio/imaging/shards.py`),
1 to 64 concurrent requests, per level and for empty vs tissue chunks; JPEG
(Pillow vs libjpeg-turbo) and lossless (PNG vs WebP) encode cost. Pass: no
throughput collapse under concurrency; server p95 per 512 px tile fits the
100 ms budget.
