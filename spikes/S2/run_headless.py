"""Run one bench page (S2, S3) in headless Firefox and wait for its result file.

    python3 -I spikes/S2/run_headless.py URL [--timeout S] [--results DIR]

The page posts its result to the server (--spike-results DIR); this script
waits until a new ``*-s<N>-<bench>.json`` appears there, then closes Firefox.
Headless Firefox ticks frames on a fixed ~17 ms timer, so frame intervals
from it are a ceiling check only; judge 60 fps in a headed browser.
"""

import argparse
import pathlib
import shutil
import subprocess
import tempfile
import time
from urllib.parse import parse_qs, urlparse

PREFS = """user_pref("webgl.force-enabled", true);
user_pref("datareporting.policy.dataSubmissionEnabled", false);
user_pref("toolkit.telemetry.enabled", false);
user_pref("app.update.enabled", false);
user_pref("browser.shell.checkDefaultBrowser", false);
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("url")
    ap.add_argument("--timeout", type=float, default=300)
    ap.add_argument("--results", type=pathlib.Path, default=pathlib.Path("spikes/S2/results"))
    ap.add_argument("--size", default="1600,1000")
    a = ap.parse_args()
    bench = parse_qs(urlparse(a.url).query).get("bench", ["pan"])[0]
    pattern = f"*-s[0-9]-{bench}.json"
    a.results.mkdir(parents=True, exist_ok=True)
    before = set(a.results.glob(pattern))
    prof = pathlib.Path(tempfile.mkdtemp(prefix="cs-ff-"))
    try:
        (prof / "user.js").write_text(PREFS)
        with open(prof / "stderr.log", "w") as err:
            p = subprocess.Popen(["firefox", "--headless", "--no-remote", "--profile", str(prof),
                                  f"--window-size={a.size}", a.url],
                                 stdout=subprocess.DEVNULL, stderr=err)
            t0, new = time.time(), set()
            while time.time() - t0 < a.timeout and not new:
                time.sleep(1)
                new = set(a.results.glob(pattern)) - before
            p.terminate()
            p.wait(10)
        print(f"{time.time() - t0:.0f}s", *sorted(n.name for n in new) or ["no result"])
        if not new:
            print((prof / "stderr.log").read_text()[-3000:])
    finally:
        shutil.rmtree(prof, ignore_errors=True)


if __name__ == "__main__":
    main()
