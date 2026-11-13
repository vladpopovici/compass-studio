# S1: tile throughput

Readers (direct shard reader vs zarr-python), encoders, and HTTP load at 1 to
64 concurrent requests on the real slide.

    cd server
    uv run python ../spikes/S1/bench.py tiles <image.zarr>
    taskset -c 0-3 uv run python ../spikes/S1/bench.py inproc <image.zarr>
    python3 -I ../spikes/S1/run_http.py <image.zarr> fast --reader auto
    python3 -I ../spikes/S1/run_http.py <image.zarr> zarr --reader zarr

`run_http.py` pins the server to CPUs 0-3 and the load generator (4 client
processes) to CPUs 4-7. Results: `docs/spikes/S1.md`.
