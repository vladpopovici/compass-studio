"""Minimal WebDriver BiDi client for headless Firefox (no geckodriver).

Firefox speaks WebDriver BiDi natively with ``--remote-debugging-port``.
Used by the end-to-end smoke test; needs only ``websockets`` (in the server
environment via ``uvicorn[standard]``).
"""

import asyncio
import base64
import itertools
import json
import pathlib
import shutil
import socket
import subprocess
import tempfile

import websockets

PREFS = """user_pref("webgl.force-enabled", true);
user_pref("datareporting.policy.dataSubmissionEnabled", false);
user_pref("toolkit.telemetry.enabled", false);
user_pref("app.update.enabled", false);
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("remote.prefs.recommended", true);
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Firefox:
    def __init__(self, width: int = 1400, height: int = 900):
        self.size = (width, height)
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self.events: list[dict] = []

    async def __aenter__(self):
        self.profile = pathlib.Path(tempfile.mkdtemp(prefix="cs-bidi-"))
        (self.profile / "user.js").write_text(PREFS)
        port = _free_port()
        self.proc = subprocess.Popen(
            ["firefox", "--headless", "--no-remote", "--profile", str(self.profile),
             f"--remote-debugging-port={port}", f"--window-size={self.size[0]},{self.size[1]}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                self.ws = await websockets.connect(f"ws://127.0.0.1:{port}/session", max_size=None)
                break
            except OSError:
                await asyncio.sleep(0.2)
        else:
            raise RuntimeError("Firefox did not open its BiDi port")
        self._reader = asyncio.create_task(self._read())
        await self.send("session.new", {"capabilities": {}})
        tree = await self.send("browsingContext.getTree", {})
        self.context = tree["contexts"][0]["context"]
        await self.send("session.subscribe", {"events": ["log.entryAdded"]})
        return self

    async def __aexit__(self, *exc):
        try:
            await self.send("session.end", {})
        except Exception:
            pass
        self._reader.cancel()
        await self.ws.close()
        self.proc.terminate()
        self.proc.wait(10)
        shutil.rmtree(self.profile, ignore_errors=True)

    async def _read(self):
        async for raw in self.ws:
            msg = json.loads(raw)
            if "id" in msg and msg["id"] in self._pending:
                self._pending.pop(msg["id"]).set_result(msg)
            elif msg.get("type") == "event":
                self.events.append(msg)

    async def send(self, method: str, params: dict) -> dict:
        i = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[i] = fut
        await self.ws.send(json.dumps({"id": i, "method": method, "params": params}))
        msg = await asyncio.wait_for(fut, 60)
        if msg.get("type") == "error":
            raise RuntimeError(f"{method}: {msg.get('error')}: {msg.get('message')}")
        return msg["result"]

    async def goto(self, url: str):
        await self.send("browsingContext.navigate", {"context": self.context, "url": url, "wait": "complete"})

    async def eval(self, expr: str):
        r = await self.send("script.evaluate", {"expression": expr, "target": {"context": self.context},
                                                "awaitPromise": True, "serializationOptions": {"maxDomDepth": 0}})
        if r.get("type") == "exception":
            raise RuntimeError(r["exceptionDetails"]["text"])
        return _value(r["result"])

    async def wait_for(self, expr: str, timeout: float = 30, interval: float = 0.2):
        t = asyncio.get_running_loop().time()
        while True:
            v = await self.eval(expr)
            if v:
                return v
            if asyncio.get_running_loop().time() - t > timeout:
                raise TimeoutError(expr)
            await asyncio.sleep(interval)

    async def screenshot(self, path: pathlib.Path):
        r = await self.send("browsingContext.captureScreenshot", {"context": self.context})
        path.write_bytes(base64.b64decode(r["data"]))

    async def key(self, *keys: str):
        actions = []
        for k in keys:
            actions += [{"type": "keyDown", "value": k}, {"type": "keyUp", "value": k}]
        await self.send("input.performActions", {"context": self.context, "actions": [
            {"type": "key", "id": "kbd", "actions": actions}]})

    async def mouse(self, actions: list[dict]):
        await self.send("input.performActions", {"context": self.context, "actions": [
            {"type": "pointer", "id": "mouse", "parameters": {"pointerType": "mouse"}, "actions": actions}]})

    async def wheel(self, x: int, y: int, dy: int):
        await self.send("input.performActions", {"context": self.context, "actions": [
            {"type": "wheel", "id": "wheel", "actions": [{"type": "scroll", "x": x, "y": y, "deltaX": 0, "deltaY": dy}]}]})

    async def chord(self, *keys: str):
        """Press keys together (modifiers first), release in reverse order."""
        actions = [{"type": "keyDown", "value": k} for k in keys] + [{"type": "keyUp", "value": k} for k in reversed(keys)]
        await self.send("input.performActions", {"context": self.context, "actions": [
            {"type": "key", "id": "kbd", "actions": actions}]})


def _value(v: dict):
    t = v.get("type")
    if t in ("string", "number", "boolean"):
        return v["value"]
    if t in ("undefined", "null"):
        return None
    if t == "array":
        return [_value(x) for x in v["value"]]
    if t == "object":
        return {k if isinstance(k, str) else _value(k): _value(x) for k, x in v["value"]}
    return v.get("value")
