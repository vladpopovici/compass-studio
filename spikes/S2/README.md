# S2: deck.gl tiles

OrthographicView and TileLayer on the real slide and on a procedural 200k px
pyramid. Pages: `web/src/pan.ts` (scripted flight, frame and tile timings),
`web/src/jitter.ts` (edge placement at large coordinates and deep zoom).
`run_headless.py` drives them in headless Firefox. Pass: 60 fps pan/zoom on
the reference laptop; no visible jitter.

Results and how to run on the laptop: `docs/spikes/S2.md`.
