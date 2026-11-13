# S5: editing on the orthographic canvas

editable-layers (ModifyMode, DrawPolygonMode) over 1M resident polygons,
with check-out and commit through the draft edit API. Page:
`web/src/edit.ts` (`bench=edit`).

    cd server
    uv run python -c "from compass_studio.annot.sidecar import Sidecar; \
        s = Sidecar('../data/s5/edit.sqlite'); \
        [s.fork_layer('../data/synth/full.ann.sqlite', l) for l in (2, 3)]"
    uv run compass-studio-serve --spike-files ../data/synth --sidecar ../data/s5/edit.sqlite \
        --web ../web/dist --spike-results ../spikes/S5/results
    python3 -I spikes/S2/run_headless.py "http://127.0.0.1:8765/?bench=edit#token=<token>" \
        --results spikes/S5/results

Results: `docs/spikes/S5.md`.
