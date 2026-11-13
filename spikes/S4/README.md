# S4: SQLite read path

Per-tile R-tree query, WKB decode, earcut and Arrow encode on the synthetic
stores, by level, placement, object order and thread count.

    cd server
    uv run compass-studio-synth ../data/synth --name full
    uv run compass-studio-synth ../data/synth --name fullrand --order random --no-arrow
    taskset -c 0-3 uv run python ../spikes/S4/bench.py \
        ../data/synth/full.ann.sqlite ../data/synth/fullrand.ann.sqlite

Results: `docs/spikes/S4.md`.
