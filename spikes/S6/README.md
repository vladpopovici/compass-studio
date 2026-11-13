# S6: sidecar write path

Fork cost, commit latency by batch size and operation, detection of another
process's commits, two writers.

    cd server
    taskset -c 0-3 uv run python ../spikes/S6/bench.py ../data/synth/full.ann.sqlite ../data/s6work

Results: `docs/spikes/S6.md`.
