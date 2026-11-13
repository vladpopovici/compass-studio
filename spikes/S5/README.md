# S5: editing on the orthographic canvas

editable-layers (ModifyMode, DrawPolygonMode) over 1M resident polygons,
with check-out and commit through the draft edit API. Page:
`web/src/bench/edit.ts` (`bench=edit`).

    cd server
    uv run compass-studio-serve --annotations synth=../data/synth/full.ann.sqlite \
        --sidecar-dir ../data/s5v1 --spike-files ../data/synth \
        --web ../web/dist --spike-results ../spikes/S5/results
    python3 -I spikes/S2/run_headless.py "http://127.0.0.1:8765/bench.html?bench=edit#token=<token>" \
        --results spikes/S5/results

The page forks the cells and regions layers through the v1 API on first use.

Results: `docs/spikes/S5.md`.
