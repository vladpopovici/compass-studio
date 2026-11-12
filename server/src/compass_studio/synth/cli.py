"""``compass-studio-synth``: write synthetic annotations for the spikes."""

import argparse
import time
from dataclasses import fields
from pathlib import Path

from .generate import Params, generate


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("out_dir", type=Path, help="output directory (keep it under data/)")
    ap.add_argument("--name", default="synth", help="file stem (default: synth)")
    ap.add_argument("--no-sqlite", action="store_true")
    ap.add_argument("--no-arrow", action="store_true")
    for f in fields(Params):
        flag = "--" + f.name.replace("_", "-")
        if isinstance(f.default, tuple):
            ap.add_argument(flag, type=type(f.default[0]), nargs=2, default=f.default,
                            metavar=("MIN", "MAX"))
        else:
            ap.add_argument(flag, type=type(f.default), default=f.default)
    a = ap.parse_args(argv)
    p = Params(**{f.name: (tuple(v) if isinstance(v := getattr(a, f.name), list) else v)
                  for f in fields(Params)})
    t0 = time.perf_counter()
    generate(a.out_dir, a.name, p, sqlite=not a.no_sqlite, arrow=not a.no_arrow,
             log=lambda m: print(f"[{time.perf_counter() - t0:7.1f}s] {m}", flush=True))
