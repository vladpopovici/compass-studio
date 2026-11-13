# Spikes

Throwaway harnesses for the questions in `docs/decisions.md` section 6. Code
that should survive lives in `server/` or `web/`; harnesses here only drive
and measure it. Raw outputs go to `spikes/Sn/results/` (gitignored); each
spike's summary goes to `docs/spikes/Sn.md` and its outcome to the decision
log.

Inputs come from the gitignored `data/` directory and are passed as
arguments; never commit slide paths or data.

Machines: since 2026-11-13 all spike runs are on the workstation (S2 was also
run on the reference laptop), limited to 4 CPUs and 32 GB RAM:

    systemd-run --user --scope -p CPUQuota=400% -p MemoryMax=32G <command>

Where the user systemd bus is unavailable (as in sandboxed shells), use
`taskset -c 0-3 prlimit --as=34359738368 <command>`; this caps address
space rather than resident memory, so it is a looser limit.

That is representative for S1, S4 and S6; GPU frame rates (S3, S5) are
optimistic, an accepted risk (decision log section 2).

Order (chosen 2026-11-12): minimal shard reader and synthetic generator, then
S2 and S3 in parallel with S4, then the full S1 benchmark.

Synthetic annotations:

    cd server
    uv run compass-studio-synth ../data/synth --name full   # 5M points, 1M cells
    uv run compass-studio-synth --help
