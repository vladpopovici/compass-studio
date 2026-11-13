"""Start compass-studio-serve (4 CPUs), load it from other CPUs, stop it.

    cd server && python3 -I ../spikes/S1/run_http.py IMAGE LABEL [server args...]

Client: S1_CLIENT_PROCS processes (default 4), format S1_FMT (default jpg).
"""

import os
import subprocess
import sys
import time
import urllib.request

image, label, extra = sys.argv[1], sys.argv[2], sys.argv[3:]
client = ["--procs", os.environ.get("S1_CLIENT_PROCS", "4"), "--fmt", os.environ.get("S1_FMT", "jpg")]
port, token = "8771", "s1-local"
env = {**os.environ, "COMPASS_STUDIO_TOKEN": token}
srv = subprocess.Popen(["taskset", "-c", "0-3", "uv", "run", "compass-studio-serve",
                        "--image", f"sample={image}", "--port", port, *extra],
                       env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(80):
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/v1/info",
                                         headers={"Authorization": f"Bearer {token}"})
            urllib.request.urlopen(req, timeout=1)
            break
        except OSError:
            time.sleep(0.25)
    subprocess.run(["taskset", "-c", "4-7", "uv", "run", "python", "../spikes/S1/bench.py", "http",
                    f"http://127.0.0.1:{port}", token, "sample", "--label", label, *client], check=True)
finally:
    srv.terminate()
    srv.wait(10)
