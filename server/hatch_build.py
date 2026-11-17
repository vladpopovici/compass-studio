"""Wheel build hook: bundles the built browser client as ``compass_studio/web``.

The client is built with ``pnpm build`` in ``../web`` (Node and pnpm are needed
to build the wheel, not to install it). Set ``COMPASS_STUDIO_WEB_DIST`` to an
already built client directory to skip the build. Editable installs (``uv
sync``) bundle nothing: they use ``web/dist`` of the checkout.

Source maps and the spike benches (``bench.html``) are left out.
"""

import os
import shutil
import subprocess
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class WebClient(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict) -> None:
        if self.target_name != "wheel" or version == "editable":
            return
        given = os.environ.get("COMPASS_STUDIO_WEB_DIST")
        if given:
            dist = Path(given)
        else:
            web = Path(self.root).parent / "web"
            if not (web / "package.json").is_file():
                raise RuntimeError(
                    f"{web}: no client sources (build the wheel from a repository checkout, "
                    "not from an sdist: uv build --wheel), or set COMPASS_STUDIO_WEB_DIST")
            pnpm = shutil.which("pnpm")
            if pnpm is None:
                raise RuntimeError("pnpm is needed to build the client into the wheel "
                                   "(or set COMPASS_STUDIO_WEB_DIST to a built client)")
            subprocess.run([pnpm, "install", "--frozen-lockfile"], cwd=web, check=True)
            subprocess.run([pnpm, "build"], cwd=web, check=True)
            dist = web / "dist"
        if not (dist / "index.html").is_file():
            raise RuntimeError(f"{dist}: no index.html")
        for f in sorted(dist.rglob("*")):
            rel = f.relative_to(dist).as_posix()
            if not f.is_file() or f.suffix == ".map" or rel == "bench.html" or rel.startswith("assets/bench-"):
                continue
            build_data["force_include"][str(f)] = f"compass_studio/web/{rel}"
