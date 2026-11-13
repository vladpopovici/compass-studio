"""End-to-end smoke test of the milestone 3 viewer in headless Firefox.

    cd server && uv run python ../web/e2e/smoke.py http://127.0.0.1:8765 TOKEN [--shots DIR]

Needs a running server with ``--web ../web/dist`` and at least two images.
Checks: tiles load, keyboard zoom/fit/1:1, mouse drag and wheel, cursor
coordinates, the view kept in the URL across a reload, image switching, the
error shown without a token, and no console errors. If the first image has
label maps: the panel, label values under the cursor, `l` and class toggles.
"""

import argparse
import asyncio
import pathlib
import sys
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from bidi import Firefox  # noqa: E402

TILES_IDLE = """(() => { const t = document.querySelector('footer [title="Tile loading"]')?.textContent ?? '';
  const m = t.match(/tiles (\\d+)\\/(\\d+) inflight (\\d+)/); return !!m && +m[1] > 0 && +m[3] === 0; })()"""


async def url_view(ff) -> dict:
    q = parse_qs(urlparse(await ff.eval("location.href")).query)
    return {k: (float(v[0]) if k in "xyz" else v[0]) for k, v in q.items()}


async def settle(ff, before: dict | None = None, timeout: float = 10) -> dict:
    """Wait until the URL (written 300 ms after the view settles) changes and is stable."""
    t0 = asyncio.get_running_loop().time()
    last = None
    while asyncio.get_running_loop().time() - t0 < timeout:
        await asyncio.sleep(0.5)
        v = await url_view(ff)
        if v == last and v != before and "z" in v:
            return v
        last = v
    raise TimeoutError(f"view did not change from {before}")


HOVER = "document.querySelector('footer [title=\"Label under the cursor\"]').textContent"


async def hover_label(ff) -> tuple[int, int, str] | None:
    """Move over a grid of canvas points until a label value shows."""
    for y in range(150, 600, 50):
        for x in range(300, 1000, 50):
            await ff.mouse([{"type": "pointerMove", "x": x, "y": y}])
            await asyncio.sleep(0.05)
            t = await ff.eval(HOVER)
            if t:
                return x, y, t
    return None


async def check_labels(ff, check, shots):
    check(True, "label maps panel shown")
    await ff.wait_for(TILES_IDLE)
    await asyncio.sleep(0.5)
    if shots:
        await ff.screenshot(shots / "4-labels.png")
    hit = await hover_label(ff)
    check(hit is not None, f"label value under the cursor: {hit}")
    if not hit:
        return
    x, y, _ = hit
    await ff.key("l")
    await asyncio.sleep(0.3)
    await ff.mouse([{"type": "pointerMove", "x": x + 1, "y": y}])
    await asyncio.sleep(0.2)
    check(await ff.eval(HOVER) == "", "'l' hides the label maps")
    await ff.key("l")
    await ff.key("o")
    await asyncio.sleep(0.5)
    if shots:
        await ff.wait_for(TILES_IDLE)
        await ff.screenshot(shots / "5-outlines.png")
    await ff.key("o")
    n = await ff.eval("document.querySelectorAll('aside input[type=checkbox]').length")
    await ff.eval("document.querySelector('aside button:nth-of-type(1)') && 1")
    none = await ff.eval("(() => { const b = [...document.querySelectorAll('aside button')].find(b => b.textContent === 'None');"
                         " b.click(); return 1; })()")
    await asyncio.sleep(0.3)
    checked = await ff.eval("[...document.querySelectorAll('aside input[type=checkbox]')].filter(c => c.checked).length")
    check(none and n > 0 and checked == 0, f"'None' unchecks all {n} classes")
    await ff.eval("[...document.querySelectorAll('aside button')].find(b => b.textContent === 'All').click(), 1")
    await asyncio.sleep(0.3)
    checked = await ff.eval("[...document.querySelectorAll('aside input[type=checkbox]')].filter(c => c.checked).length")
    check(checked == n, "'All' checks them again")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("base")
    ap.add_argument("token")
    ap.add_argument("--shots", type=pathlib.Path)
    a = ap.parse_args()
    ok = True

    def check(cond, what):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + what)
        ok &= bool(cond)

    async with Firefox() as ff:
        await ff.goto(f"{a.base}/#token={a.token}")
        await ff.wait_for(TILES_IDLE)
        v0 = await settle(ff)
        check(v0.get("image"), f"first image opens fitted: {v0}")
        if a.shots:
            a.shots.mkdir(parents=True, exist_ok=True)
            await ff.screenshot(a.shots / "1-fit.png")

        labels = await ff.eval("document.querySelector('aside h2')?.textContent ?? null")
        if labels:
            await check_labels(ff, check, a.shots)
            v0 = await url_view(ff)

        await ff.key("1")
        v = await settle(ff, v0)
        check(abs(v["z"]) < 0.01, f"'1' zooms to 1:1 (z={v['z']})")
        level = await ff.eval("document.querySelector('footer [title=\"Pyramid level shown\"]').textContent")
        check(level.startswith("L0/"), f"level 0 shown at 1:1 ({level})")
        await ff.wait_for(TILES_IDLE)
        if a.shots:
            await ff.screenshot(a.shots / "2-one-to-one.png")

        await ff.key("0")
        v = await settle(ff, v)
        check(abs(v["z"] - v0["z"]) < 0.01, "'0' fits again")
        await ff.key("+")
        v = await settle(ff, v)
        check(abs(v["z"] - (v0["z"] + 1)) < 0.01, "'+' zooms in by one level")
        await ff.key("")  # ArrowRight
        v2 = await settle(ff, v)
        check(v2["x"] > v["x"], "ArrowRight pans right")

        await ff.mouse([{"type": "pointerMove", "x": 700, "y": 450}, {"type": "pointerDown", "button": 0},
                        {"type": "pointerMove", "x": 500, "y": 350, "duration": 300},
                        {"type": "pointerUp", "button": 0}])
        v3 = await settle(ff, v2)
        check(v3["x"] > v2["x"] and v3["y"] > v2["y"], "dragging moves the view")
        await ff.wheel(700, 450, -600)
        v4 = await settle(ff, v3)
        check(v4["z"] > v3["z"], f"wheel zooms in ({v3['z']} -> {v4['z']})")

        # left of centre: the drag above pushed the view centre to the image's right edge
        await ff.mouse([{"type": "pointerMove", "x": 300, "y": 300}, {"type": "pointerMove", "x": 400, "y": 400}])
        await asyncio.sleep(0.3)
        cur = await ff.eval("document.querySelector('footer [title=\"Cursor, level-0 px\"]').textContent")
        um = await ff.eval("document.querySelector('footer [title=\"Cursor, microns\"]').textContent")
        check(cur.endswith(" px"), f"cursor coordinates shown: {cur} {um}")
        bar = await ff.eval("document.querySelector('footer [title=\"Scale\"]')?.textContent")
        check(bool(bar), f"scale bar shown: {bar}")
        if a.shots:
            await ff.wait_for(TILES_IDLE)
            await ff.screenshot(a.shots / "3-zoomed.png")

        href = await ff.eval("location.href")
        await ff.goto(href.split("#")[0] + "#token=" + a.token)
        await ff.wait_for(TILES_IDLE)
        await asyncio.sleep(1)
        v5 = await url_view(ff)
        check(all(abs(v5[k] - v4[k]) < 1.01 for k in "xy") and abs(v5["z"] - v4["z"]) < 0.011,
              f"reload restores the view ({v4} -> {v5})")

        n = await ff.eval("fetch('api/v1/images', {headers: {Authorization: 'Bearer " + a.token + "'}})"
                          ".then(r => r.json()).then(j => j.length)")
        if n >= 2:
            await ff.key("")  # PageDown
            v6 = await settle(ff, v5)
            check(v6["image"] != v5["image"], f"PageDown switches image ({v5['image']} -> {v6['image']})")
        else:
            print("SKIP image switching (one image served)")

        errors = [e["params"] for e in ff.events
                  if e.get("method") == "log.entryAdded" and e["params"].get("level") == "error"]
        check(not errors, f"no console errors {[e.get('text') for e in errors][:3]}")

        await ff.goto(f"{a.base}/")
        msg = await ff.wait_for("(() => { const t = document.querySelector('main')?.textContent ?? '';"
                                " return t.includes('token') ? t : null; })()")
        check("token" in msg, f"without a token: {msg!r}")
    print("OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
