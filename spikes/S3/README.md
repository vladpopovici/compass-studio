# S3: annotation scale

5M points and 1M polygons from the synthetic Arrow files, binary attributes,
group toggles, picking. Page: `web/src/annot.ts` (`bench=annot`).

    cd server && uv run compass-studio-synth ../data/synth --name full
    uv run compass-studio-serve --spike-files ../data/synth --web ../web/dist \
        --spike-results ../spikes/S3/results
    python3 -I spikes/S2/run_headless.py \
        "http://127.0.0.1:8765/?bench=annot#token=<token>" --results spikes/S3/results

Options: `layers=points,cells,regions`, `tess=server|client`,
`polymode=groups|filter`, `set=<file stem>`. Results: `docs/spikes/S3.md`.
