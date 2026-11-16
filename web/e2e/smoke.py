"""End-to-end smoke test of the viewer in headless Firefox.

    cd server && uv run python ../web/e2e/smoke.py http://127.0.0.1:8765 TOKEN [--shots DIR]

Needs a running server with ``--web ../web/dist`` and at least two images.
Checks: tiles load, keyboard zoom/fit/1:1, mouse drag and wheel, cursor
coordinates, the view kept in the URL across a reload, image switching, the
error shown without a token, and no console errors. If the first image has
label maps: the panel, label values under the cursor, `l` and class toggles.
If it has a point layer with at least 1000 points: hover, click selection
and the inspector's attributes, Shift+click, Esc, and group toggles. If it
has a polygon layer with at least 100 polygons: shapes zoomed in (hover,
click, inspector, outline and fill controls) and centres zoomed out; with
a polyline layer, its line width control.
"""

import argparse
import asyncio
import json
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


POINT_HOVER = "document.querySelector('footer [title=\"Annotation object under the cursor\"]').textContent"
# the point layer with the most points
LAYER = ("[...document.querySelectorAll('[data-points]')].filter(e => +e.dataset.points > 0)"
         ".sort((a, b) => b.dataset.points - a.dataset.points)[0]")


async def hover_point(ff, avoid: str = "", prefix: str = "") -> tuple[int, int, str] | None:
    for y in range(120, 620, 9):
        for x in range(320, 1300, 9):
            await ff.mouse([{"type": "pointerMove", "x": x, "y": y}])
            t = await ff.eval(POINT_HOVER)
            if t and t != avoid and t.startswith(prefix):
                # hover picks on the next frame: confirm at rest before using it
                await asyncio.sleep(0.15)
                t = await ff.eval(POINT_HOVER)
                if t and t != avoid and t.startswith(prefix):
                    return x, y, t
    return None


async def check_points(ff, check, shots):
    """Point layers of the first image: drawn, picked, inspected, filtered."""
    await ff.wait_for(f"{LAYER} && !document.querySelector('[aria-label=\"Loading\"]') && 1", 120)
    layer = await ff.eval(f"{LAYER}.dataset.layer")
    check(True, f"point layer {layer} loaded")
    hint = f"{LAYER}.querySelector('[data-size-hint]')"
    h = await ff.eval(f"{hint}.dataset.band + ': ' + {hint}.textContent")
    check(h.startswith("low"), f"point size band at fit: {h}")
    await ff.key("1")
    await asyncio.sleep(0.5)
    h = await ff.eval(f"{hint}.dataset.band + ': ' + {hint}.textContent")
    check(h.startswith("high"), f"point size band at 1:1: {h}")
    await ff.key("0")
    await asyncio.sleep(0.5)
    await ff.key("+")
    await ff.key("+")
    await asyncio.sleep(1)
    await ff.wait_for(TILES_IDLE)
    hit = await hover_point(ff)
    check(hit is not None, f"hovering a point names it: {hit}")
    if not hit:
        return
    x, y, _ = hit
    await ff.mouse([{"type": "pointerDown", "button": 0}, {"type": "pointerUp", "button": 0}])
    insp = await ff.wait_for("(() => { const a = document.querySelector('aside[aria-label=Inspector]');"
                             " const t = a?.innerText ?? ''; return t.includes('Id') && !t.includes('Loading attributes')"
                             " ? t : null; })()", 15)
    check("Group" in insp and ("Position" in insp or "Centre" in insp),
          f"click opens the inspector: {' '.join(insp.split())[:120]}")
    if shots:
        await ff.screenshot(shots / "6-selected.png")
    hit2 = await hover_point(ff, avoid=hit[2])
    if hit2:
        await ff.send("input.performActions", {"context": ff.context, "actions": [
            {"type": "key", "id": "kbd", "actions": [{"type": "keyDown", "value": "\ue008"}]},
            {"type": "pointer", "id": "mouse", "parameters": {"pointerType": "mouse"}, "actions": [
                {"type": "pause"}, {"type": "pointerDown", "button": 0}, {"type": "pointerUp", "button": 0}]}]})
        await ff.send("input.releaseActions", {"context": ff.context})
        await asyncio.sleep(0.5)
        n = await ff.eval("document.querySelectorAll('aside[aria-label=Inspector] [data-object]').length")
        check(n == 2, f"Shift+click adds a second object ({n} selected)")
    await ff.key("\ue00c")  # Escape
    await asyncio.sleep(0.3)
    n = await ff.eval("document.querySelectorAll('aside[aria-label=Inspector] [data-object]').length")
    check(n == 0, "Esc clears the selection")
    await ff.eval(f"[...{LAYER}.querySelectorAll('button')].find(b => b.textContent === 'None').click(), 1")
    await asyncio.sleep(0.5)
    await ff.mouse([{"type": "pointerMove", "x": x + 1, "y": y}, {"type": "pointerMove", "x": x, "y": y}])
    await asyncio.sleep(0.3)
    t = await ff.eval(POINT_HOVER)
    check(not t.startswith(hit[2].split(" › ")[0] + " ›"), f"'None' hides every group (nothing of the layer to pick: {t!r})")
    await ff.eval(f"[...{LAYER}.querySelectorAll('button')].find(b => b.textContent === 'All').click(), 1")
    await ff.key("]")  # close the inspector
    await ff.key("0")
    await asyncio.sleep(0.5)


POLYS = ("[...document.querySelectorAll('[data-polygons]')].filter(e => +e.dataset.polygons > 0)"
         ".sort((a, b) => b.dataset.polygons - a.dataset.polygons)[0]")


async def click_buttons(ff, text: str, but: str = ""):
    """Click every layer card's button labelled `text`, except in the card `but`."""
    await ff.eval(f"[...document.querySelectorAll('[data-layer]')].filter(c => c.dataset.layer !== '{but}')"
                  f".forEach(c => [...c.querySelectorAll('button')].find(b => b.textContent === '{text}')?.click()), 1")
    await asyncio.sleep(0.5)


def poly_stats(layer: str) -> str:
    return f"JSON.stringify(window.__cs.ann.sources.current.get('{layer}').polygons.stats)"


SET_RANGE = ("((el, v) => { Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(el, v);"
             " el.dispatchEvent(new Event('input', { bubbles: true })); return 1; })")


async def check_polygons(ff, check, shots):
    """Polygons by on-screen size: small ones as centres, large ones as shapes."""
    layer = await ff.eval(f"{POLYS}.dataset.layer")
    name = await ff.eval(f"{POLYS}.querySelector('span.font-medium').textContent")
    await ff.key("0")
    await asyncio.sleep(1.5)
    await ff.wait_for(TILES_IDLE)
    st = json.loads(await ff.eval(poly_stats(layer)))
    check(st["centres"] > 0 and st["shapes"] < st["centres"], f"most of {name} drawn as centres at fit: {st}")
    others = await ff.eval("[...document.querySelectorAll('[data-polygons]')].filter(e => +e.dataset.polygons > 0)"
                           f".map(e => e.dataset.layer).filter(l => l !== '{layer}')")
    if others:  # e.g. tissue regions: large enough to stay shapes at fit
        big = [json.loads(await ff.eval(poly_stats(o))) for o in others]
        check(any(b["shapes"] > 0 for b in big), f"large polygons drawn as shapes at fit: {big}")
    await click_buttons(ff, "None", but=layer)  # only this layer's objects can be picked
    centre = await hover_point(ff, prefix=f"{name} ›")
    check(centre is not None, f"hovering a polygon centre names it: {centre}")
    if centre:  # zoom in around it (the wheel keeps the point under the cursor)
        for _ in range(7):
            await ff.wheel(centre[0], centre[1], -600)
            await asyncio.sleep(0.4)
        await asyncio.sleep(1.5)
    await ff.wait_for(TILES_IDLE)
    await ff.wait_for(f"window.__cs.ann.sources.current.get('{layer}').polygons.stats.pending === 0", 30)
    await asyncio.sleep(0.3)
    st = json.loads(await ff.eval(poly_stats(layer)))
    check(st["shapes"] > 0, f"polygons drawn as shapes zoomed in: {st}")
    # the cutoff slider moves polygons between shapes and centres, without a refetch here
    slider = f"{POLYS}.querySelector('[aria-label=\"Shapes from (px)\"]')"
    await ff.eval(f"{SET_RANGE}({slider}, 64)")
    await asyncio.sleep(0.5)
    st2 = json.loads(await ff.eval(poly_stats(layer)))
    check(st2["shapes"] < st["shapes"] and st2["polygons"] == st["polygons"],
          f"'Shapes from' 64 px turns small polygons into centres: {st['shapes']} -> {st2['shapes']} shapes")
    await ff.eval(f"{SET_RANGE}({slider}, 2)")
    await asyncio.sleep(0.5)
    check(await ff.eval(f"!!{POLYS}.querySelector('[aria-label=\"Fill opacity\"]') && "
                        f"!!{POLYS}.querySelector('[aria-label=\"Outlines\"]')"), "fill and outline controls shown")
    hit = await hover_point(ff, prefix=f"{name} ›")
    check(hit is not None, f"hovering a polygon names it: {hit}")
    if hit:
        await ff.mouse([{"type": "pointerDown", "button": 0}, {"type": "pointerUp", "button": 0}])
        insp = await ff.wait_for("(() => { const a = document.querySelector('aside[aria-label=Inspector]');"
                                 " const t = a?.innerText ?? ''; return t.includes('Centre') && !t.includes('Loading attributes')"
                                 " ? t : null; })()", 15)
        check("polygon" in insp, f"click selects the polygon: {' '.join(insp.split())[:120]}")
        check("Area" in insp and "Perimeter" in insp and "µm²" in insp, "inspector shows area and perimeter")
        if shots:
            await ff.screenshot(shots / "7-polygon.png")
        await ff.key("\ue00c")  # Escape
        await ff.key("]")
    await ff.eval(f"{POLYS}.querySelector('[aria-label=\"Outlines\"]').click(), 1")
    await asyncio.sleep(0.3)
    check(not await ff.eval(f"{POLYS}.querySelector('[aria-label=\"Outlines\"]').checked"), "outline toggles off")
    await ff.eval(f"{POLYS}.querySelector('[aria-label=\"Outlines\"]').click(), 1")
    lines = await ff.eval("[...document.querySelectorAll('[data-lines]')].filter(e => +e.dataset.lines > 0).length")
    if lines:
        check(await ff.eval("[...document.querySelectorAll('[data-lines]')].filter(e => +e.dataset.lines > 0)"
                            ".every(e => e.querySelector('[aria-label=\"Lines (µm)\"]'))"), f"line width shown for {lines} polyline layer(s)")
    await ff.key("0")
    await asyncio.sleep(1)
    await click_buttons(ff, "All")
    await asyncio.sleep(0.5)


async def check_labels(ff, check, shots):
    # a real pointer click: Radix tabs switch on mousedown, not on a synthetic click()
    r = await ff.eval("(() => { const b = [...document.querySelectorAll('[role=tab]')].find(t => t.textContent.startsWith('Labels'))"
                      ".getBoundingClientRect(); return [Math.round(b.x + b.width / 2), Math.round(b.y + b.height / 2)]; })()")
    await ff.mouse([{"type": "pointerMove", "x": r[0], "y": r[1]}, {"type": "pointerDown", "button": 0},
                    {"type": "pointerUp", "button": 0}])
    await asyncio.sleep(0.3)
    check(await ff.eval("document.querySelector('[role=tab][aria-selected=true]')?.textContent.startsWith('Labels')"),
          "label maps tab shown")
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
    n = await ff.eval("document.querySelectorAll('aside [role=tabpanel] input[type=checkbox]').length")
    none = await ff.eval("(() => { const b = [...document.querySelectorAll('aside [role=tabpanel] button')].find(b => b.textContent === 'None');"
                         " b.click(); return 1; })()")
    await asyncio.sleep(0.3)
    checked = await ff.eval("[...document.querySelectorAll('aside [role=tabpanel] input[type=checkbox]')].filter(c => c.checked).length")
    check(none and n > 0 and checked == 0, f"'None' unchecks all {n} classes")
    await ff.eval("[...document.querySelectorAll('aside [role=tabpanel] button')].find(b => b.textContent === 'All').click(), 1")
    await asyncio.sleep(0.3)
    checked = await ff.eval("[...document.querySelectorAll('aside [role=tabpanel] input[type=checkbox]')].filter(c => c.checked).length")
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
        await ff.goto(f"{a.base}/?debug#token={a.token}")
        await ff.wait_for(TILES_IDLE)
        v0 = await settle(ff)
        check(v0.get("image"), f"first image opens fitted: {v0}")
        if a.shots:
            a.shots.mkdir(parents=True, exist_ok=True)
            await ff.screenshot(a.shots / "1-fit.png")

        tabs = await ff.eval("[...document.querySelectorAll('[role=tab]')].map(t => t.textContent).join('|')")
        most = await ff.eval(f"+({LAYER}?.dataset.points ?? 0)")
        if most >= 1000:
            await check_points(ff, check, a.shots)
            v0 = await url_view(ff)
        else:
            print(f"SKIP points (largest point layer has {most} points; needs 1000 to find one by hovering)")
        polys = await ff.eval(f"+({POLYS}?.dataset.polygons ?? 0)")
        if polys >= 100:
            await check_polygons(ff, check, a.shots)
            v0 = await url_view(ff)
        else:
            print(f"SKIP polygons (largest polygon layer has {polys} polygons; needs 100)")
        if "Labels 0" not in tabs:
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
