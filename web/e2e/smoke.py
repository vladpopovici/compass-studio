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
click, inspector, outline and fill controls, the outline's closing edge,
the working layer: lock, chip, `g`, drawing order, dimming and picking)
and centres zoomed out; with a polyline layer, its line width control. With
a multichannel image: the Channels tab, one layer and one request stream
per shown channel, contrast without refetching, values under the cursor,
Auto, the 8-channel limit (if an image has more than 8), memory and Reset.

With ``--edit`` (start the server with ``--sidecar-dir`` pointing at a
scratch directory: the test writes to the curation databases), editing on
the store of the largest polygon layer: a new layer, every drawing tool,
undo and redo, vertex editing and moving, group and attribute changes,
deleting, a commit by another client arriving through the event stream,
forking a read-only layer on first edit, and the undo stack after a reload.
"""

import argparse
import asyncio
import json
import pathlib
import sys
import time
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
    await check_outline_closed(ff, check, layer)
    await check_working_layer(ff, check, shots, layer)
    lines = await ff.eval("[...document.querySelectorAll('[data-lines]')].filter(e => +e.dataset.lines > 0).length")
    if lines:
        check(await ff.eval("[...document.querySelectorAll('[data-lines]')].filter(e => +e.dataset.lines > 0)"
                            ".every(e => e.querySelector('[aria-label=\"Lines (µm)\"]'))"), f"line width shown for {lines} polyline layer(s)")
    await ff.key("0")
    await asyncio.sleep(1)
    await click_buttons(ff, "All")
    await asyncio.sleep(0.5)


OUTLINE_PICK = """((L) => {
  const src = window.__cs.ann.sources.current.get(L).polygons, v = window.__cs.viewer, vp = v.deck.getViewports()[0];
  for (const s of src.shapes) {
    const r = s.rings, P = r.positions;
    for (let i = 0; i < r.count; i++) {
      const a = r.starts[i], b = r.starts[i + 1] - 1;
      if (b - a < 2) continue;
      const [ax, ay] = vp.project([P[2 * a], P[2 * a + 1]]), [bx, by] = vp.project([P[2 * b], P[2 * b + 1]]);
      const x = (ax + bx) / 2, y = (ay + by) / 2;
      if (x < 20 || y < 20 || x > vp.width - 20 || y > vp.height - 20 || Math.hypot(ax - bx, ay - by) < 12) continue;
      // the middle of the edge, away from the neighbouring edges' rounded ends
      const got = [0.35, 0.5, 0.65].map(t => v.pickAt(ax + t * (bx - ax), ay + t * (by - ay))?.objectId ?? null);
      const fills = v.deck.props.layers.filter(l => /^poly-.*-\\d+$/.test(l.id)).length;
      return JSON.stringify({ want: s.objectIds[s.ringPolygon[i]], got, fills });
    }
  }
  return null; })"""


async def check_outline_closed(ff, check, layer):
    """The edge from a ring's last vertex back to its first is drawn (outline picked there, fill off)."""
    card = f"document.querySelector('[data-layer=\"{layer}\"]')"
    fill = await ff.eval(f"+{card}.querySelector('[aria-label=\"Fill opacity\"]').value")
    await ff.eval(f"{SET_RANGE}({card}.querySelector('[aria-label=\"Fill opacity\"]'), 0)")
    await asyncio.sleep(0.6)
    r = await ff.eval(f"{OUTLINE_PICK}('{layer}')")
    if r is None:
        print("SKIP closing edge (no ring with its closing edge on screen)")
    else:
        r = json.loads(r)
        check(r["fills"] == 0 and r["want"] in r["got"], f"outline drawn along the closing edge (last to first vertex): {r}")
    await ff.eval(f"{SET_RANGE}({card}.querySelector('[aria-label=\"Fill opacity\"]'), {fill})")
    await asyncio.sleep(0.3)


LAYER_POINT = """((L) => {
  const src = window.__cs.ann.sources.current.get(L).polygons, v = window.__cs.viewer, d = v.deck, vp = d.getViewports()[0];
  const r = d.getCanvas().getBoundingClientRect();
  for (const s of src.shapes) {
    const P = s.positions, I = s.indices;
    for (let k = 0; k < I.length; k += 3) {
      const c = [0, 1, 2].map(j => vp.project([P[2 * I[k + j]], P[2 * I[k + j] + 1]]));
      const x = (c[0][0] + c[1][0] + c[2][0]) / 3, y = (c[0][1] + c[1][1] + c[2][1]) / 3;
      const A = Math.abs((c[1][0] - c[0][0]) * (c[2][1] - c[0][1]) - (c[2][0] - c[0][0]) * (c[1][1] - c[0][1]));
      if (A < 40 || x < 20 || y < 20 || x > vp.width - 20 || y > vp.height - 20) continue;
      const p = v.pickAt(x, y);
      if (p && p.layerId === L) return JSON.stringify({ x, y, px: Math.round(r.left + x), py: Math.round(r.top + y), id: p.objectId });
    }
  }
  return null; })"""

# deck layers of one annotation layer: poly-, centres-, points-, lines-, overlay- followed by its id
DECK_ORDER = """((L) => JSON.stringify(window.__cs.viewer.deck.props.layers.map(l => {
  const m = l.id.match(/^(poly|centres|points|lines|overlay)-(.*?)(-\\d+(-outline)?)?$/);
  return m ? [m[2] === L, l.props.opacity, l.props.pickable] : null; }).filter(Boolean)))"""


async def check_working_layer(ff, check, shots, layer):
    """Locking a working layer: drawn on top, the only layer picked, the others dimmed; G and the chip."""
    await click_buttons(ff, "All")  # every layer's groups shown again
    other = await ff.eval(f"[...document.querySelectorAll('[data-layer]')].map(c => c.dataset.layer)"
                          f".filter(l => l !== '{layer}' && window.__cs.ann.sources.current.get(l)?.polygons?.shapes?.length)[0] ?? null")
    pb = await ff.eval(f"{LAYER_POINT}('{layer}')")
    if not other or not pb:
        print(f"SKIP working layer (needs a second polygon layer in view: {other}, and a pickable object of {layer})")
        return
    pb = json.loads(pb)
    names = {lid: await ff.eval(f"window.__cs.ann.layers.find(l => l.info.id === '{lid}').info.name") for lid in (layer, other)}
    await ff.eval(f"document.querySelector('[data-layer=\"{other}\"] button[aria-label=\"Work in this layer\"]').click(), 1")
    await asyncio.sleep(0.6)
    chip = await ff.eval("document.querySelector('[data-working-chip]')?.innerText ?? ''")
    check(names[other] in chip, f"locking a layer shows 'Working in': {' '.join(chip.split())!r}")
    got = await ff.eval(f"window.__cs.viewer.pickAt({pb['x']}, {pb['y']})?.layerId ?? null")
    check(got != layer, f"objects of other layers cannot be picked while a layer is locked (picked {got})")
    order = json.loads(await ff.eval(f"{DECK_ORDER}('{other}')"))
    mine = [i for i, (m, *_) in enumerate(order) if m]
    rest = [i for i, (m, *_) in enumerate(order) if not m]
    check(mine and rest and min(mine) > max(rest), f"the working layer is drawn above the other layers ({len(mine)} deck layers on top)")
    opacity = await ff.eval(f"window.__cs.ann.layers.find(l => l.info.id === '{layer}').style.opacity")
    dimmed = [o for m, o, pk in json.loads(await ff.eval(f"{DECK_ORDER}('{layer}')")) if m]
    check(dimmed and all(abs(o - opacity * 0.35) < 1e-6 for o in dimmed) and all(not pk for m, o, pk in order if not m),
          f"other layers dimmed to 35 % and not pickable: {dimmed}")
    if shots:
        await ff.screenshot(shots / "7b-working-layer.png")
    await ff.key("g")  # releases
    await asyncio.sleep(0.4)
    check(not await ff.eval("!!document.querySelector('[data-working-chip]')"), "G releases the working layer")
    await ff.mouse([{"type": "pointerMove", "x": pb["px"], "y": pb["py"]}, {"type": "pointerDown", "button": 0}, {"type": "pointerUp", "button": 0}])
    await asyncio.sleep(0.5)
    await ff.key("g")  # locks the selected object's layer
    await asyncio.sleep(0.6)
    chip = await ff.eval("document.querySelector('[data-working-chip]')?.innerText ?? ''")
    lock = await ff.eval(f"document.querySelector('[data-layer=\"{layer}\"]')?.dataset.working ?? null")
    check(names[layer] in chip and lock == "true", f"G locks the selected object's layer ({names[layer]})")
    await ff.eval("document.querySelector('[aria-label=\"Release working layer\"]').click(), 1")
    await asyncio.sleep(0.4)
    check(not await ff.eval("!!document.querySelector('[data-working-chip]')"), "the chip's button releases the working layer")
    await ff.key(ESC)
    if await ff.eval("!!document.querySelector('aside[aria-label=Inspector]')"):
        await ff.key("]")
    await click_buttons(ff, "None", but=layer)
    await asyncio.sleep(0.3)


CH_LAYERS = """((id) => window.__cs.viewer.deck.props.layers.map(l => l.id)
  .filter(i => i.startsWith(`channels-${id}-`) && !i.endsWith('-backdrop')).map(i => +i.split('-').pop()))"""
CH_REQUESTS = """(performance.getEntriesByType('resource').map(e => e.name).filter(n => n.includes('.bin?channel=')))"""
CH_PANEL = "document.querySelector('[role=tabpanel][data-state=active]')"


async def check_channels(ff, check, shots, base, token, mc):
    """Multichannel images (milestone 8): the Channels tab, per-channel layers and requests,
    contrast without refetching, values under the cursor, the 8-channel limit, memory, Reset."""
    info = mc
    n = len(info["channels"])
    await ff.eval(f"localStorage.removeItem('cs.channels.{info['id']}'), 1")
    await ff.goto(f"{base}/?debug&image={info['id']}#token={token}")
    await ff.wait_for(f"!!{CH_PANEL}?.querySelector('[data-channel]')", 60)
    await ff.wait_for(TILES_IDLE, 60)
    tab = await ff.eval("document.querySelector('[role=tab][data-state=active]')?.textContent ?? ''")
    rows = await ff.eval(f"{CH_PANEL}.querySelectorAll('[data-channel]').length")
    shown = json.loads(await ff.eval(f"JSON.stringify({CH_LAYERS}('{info['id']}'))"))
    want = [c for c in range(n) if c < 4] if not info.get("channel_defaults") else None
    check(tab.startswith("Channels") and rows == n, f"Channels tab lists the {n} channels ({tab!r})")
    check(want is None or shown == want, f"one tile layer per shown channel: {shown}")
    if shots:
        await ff.screenshot(shots / "12-channels-fit.png")
    await ff.key("1")
    await asyncio.sleep(1)
    await ff.wait_for(TILES_IDLE, 60)
    await ff.mouse([{"type": "pointerMove", "x": 800, "y": 400}, {"type": "pointerMove", "x": 810, "y": 410}])
    await asyncio.sleep(0.4)
    hover = await ff.eval(HOVER)
    check(info["channels"][shown[0]] in hover, f"values of the shown channels under the cursor: {hover!r}")
    if shots:
        await ff.screenshot(shots / "13-channels-1to1.png")
    # turning a channel on fetches that channel only
    off = next((c for c in range(n) if c not in shown), None)
    if off is not None:
        before = len(await ff.eval(CH_REQUESTS))
        await ff.eval(f"{CH_PANEL}.querySelector('[data-channel=\"{off}\"] input[type=checkbox]').click(), 1")
        await asyncio.sleep(0.5)
        await ff.wait_for(TILES_IDLE, 60)
        new = (await ff.eval(CH_REQUESTS))[before:]
        check(new and all(f"channel={off}&" in u or u.endswith(f"channel={off}") for u in new),
              f"turning channel {off} on fetches only that channel ({len(new)} requests)")
    # contrast: the shader's window changes, nothing is fetched
    c0 = shown[0]
    before = len(await ff.eval(CH_REQUESTS))
    await ff.eval(f"{SET_RANGE}({CH_PANEL}.querySelector('[data-channel=\"{c0}\"] input[aria-label$=\" max\"]'), 500)")
    await asyncio.sleep(0.5)
    lo_hi = json.loads(await ff.eval(f"""JSON.stringify(window.__cs.viewer.deck.layerManager.getLayers()
      .filter(l => l.id.startsWith('channels-{info['id']}-{c0}-') && l.props.plane).map(l => l.props.hi))"""))
    after = len(await ff.eval(CH_REQUESTS))
    check(lo_hi and all(h == 500 for h in lo_hi) and after == before,
          f"changing the contrast updates the tiles on the GPU without fetching ({len(lo_hi)} tiles, {after - before} requests)")
    await ff.eval(f"[...{CH_PANEL}.querySelectorAll('[data-channel=\"{c0}\"] button')].find(b => b.textContent.includes('Auto')).click(), 1")
    await asyncio.sleep(0.3)
    span = await ff.eval(f"{CH_PANEL}.querySelector('[data-channel=\"{c0}\"] .font-mono').textContent")
    check(not span.endswith("–500"), f"Auto restores the contrast from the histogram ({span})")
    # remembered in this browser across a reload; Reset forgets it
    await ff.eval(f"(() => {{ const el = {CH_PANEL}.querySelector('[data-channel=\"{c0}\"] input[type=color]');"
                  " Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(el, '#123456');"
                  " el.dispatchEvent(new Event('input', { bubbles: true })); return 1; })()")
    await asyncio.sleep(0.3)
    await ff.goto(f"{base}/?debug&image={info['id']}#token={token}")
    await ff.wait_for(f"!!{CH_PANEL}?.querySelector('[data-channel]')", 60)
    color = await ff.eval(f"{CH_PANEL}.querySelector('[data-channel=\"{c0}\"] input[type=color]').value")
    check(color == "#123456", f"channel settings are remembered across a reload ({color})")
    await ff.eval(f"[...{CH_PANEL}.querySelectorAll('button')].find(b => b.textContent === 'Reset').click(), 1")
    await asyncio.sleep(0.3)
    color = await ff.eval(f"{CH_PANEL}.querySelector('[data-channel=\"{c0}\"] input[type=color]').value")
    check(color != "#123456", f"Reset goes back to the defaults ({color})")
    await ff.wait_for(TILES_IDLE, 60)
    s = json.loads(await ff.eval("JSON.stringify(window.__cs.viewer.stats.summary())"))
    print(f"     channel tiles: {s['loaded']} loaded, to screen p50 {s['request_to_screen_ms'].get('p50')} "
          f"p95 {s['request_to_screen_ms'].get('p95')} ms, server read p50 {s['server_read_ms'].get('p50')} ms")


async def check_channel_limit(ff, check, base, token, mc):
    """At most 8 channels at once: the other checkboxes are disabled."""
    await ff.goto(f"{base}/?debug&image={mc['id']}#token={token}")
    await ff.wait_for(f"!!{CH_PANEL}?.querySelector('[data-channel]')", 60)
    for _ in range(len(mc["channels"])):  # click unchecked, enabled boxes until none is left
        if not await ff.eval(f"(() => {{ const b = [...{CH_PANEL}.querySelectorAll('[data-channel] input[type=checkbox]')]"
                             ".find(x => !x.checked && !x.disabled); if (b) b.click(); return !!b; })()"):
            break
        await asyncio.sleep(0.05)
    on = await ff.eval(f"{CH_PANEL}.querySelectorAll('[data-channel] input[type=checkbox]:checked').length")
    disabled = await ff.eval(f"{CH_PANEL}.querySelectorAll('[data-channel] input[type=checkbox]:disabled').length")
    check(on == 8 and disabled == len(mc["channels"]) - 8, f"at most 8 channels at once ({on} on, {disabled} disabled)")
    await ff.eval(f"[...{CH_PANEL}.querySelectorAll('button')].find(b => b.textContent === 'Reset').click(), 1")
    await asyncio.sleep(0.3)


ENTER, ESC, CTRL, SHIFT, ALT, DEL = "\ue007", "\ue00c", "\ue009", "\ue008", "\ue00a", "\ue017"
SET_VALUE = ("((el, v) => { const p = el.tagName === 'SELECT' ? HTMLSelectElement.prototype : HTMLInputElement.prototype;"
             " Object.getOwnPropertyDescriptor(p, 'value').set.call(el, v);"
             " el.dispatchEvent(new Event(el.tagName === 'SELECT' ? 'change' : 'input', { bubbles: true })); return 1; })")
TO_SCREEN = ("((x, y) => { const d = window.__cs.viewer.deck, [sx, sy] = d.getViewports()[0].project([x, y]);"
             " const r = d.getCanvas().getBoundingClientRect(); return [Math.round(r.left + sx), Math.round(r.top + sy)]; })")
TOAST = "document.querySelector('[role=status]')?.textContent ?? ''"
INSPECTOR = "document.querySelector('aside[aria-label=Inspector]')?.innerText ?? ''"
CHECKOUT = "JSON.stringify(window.__cs.edit.checkout?.committed.map(f => f.geometry.coordinates[0]) ?? null)"


async def click_at(ff, x, y):
    await ff.mouse([{"type": "pointerMove", "x": x, "y": y}, {"type": "pointerDown", "button": 0}, {"type": "pointerUp", "button": 0}])


async def drag(ff, a, b, steps=10):
    await ff.mouse([{"type": "pointerMove", "x": a[0], "y": a[1]}, {"type": "pointerDown", "button": 0},
                    *[{"type": "pointerMove", "x": round(a[0] + (b[0] - a[0]) * i / steps),
                       "y": round(a[1] + (b[1] - a[1]) * i / steps), "duration": 16} for i in range(1, steps + 1)],
                    {"type": "pointerUp", "button": 0}])


async def check_editing(ff, check, shots, base, token, image):
    """Editing (milestone 7) on the store of the largest polygon layer; see the module docstring."""
    await ff.goto(f"{base}/?debug&image={image}#token={token}")
    await ff.wait_for("!!document.querySelector('[role=toolbar][aria-label=\"Edit tools\"]') && !document.querySelector('[aria-label=\"Loading\"]')"
                      f" && !!{POLYS}", 120)
    await asyncio.sleep(1)
    store = await ff.eval(f"window.__cs.ann.layers.find(l => l.info.id === {POLYS}.dataset.layer).info.store_id")
    overlay = ("window.__cs.edit.overlayEntries.filter(e => e.source.layerId === window.__cs.edit.target?.layerId)"
               ".reduce((a, e) => a + e.source.count, 0)")

    async def settle_edit():
        await asyncio.sleep(0.3)
        await ff.wait_for("!document.querySelector('[role=dialog]') || !!document.querySelector('[role=dialog][aria-label=\"Edit a read-only layer\"]')", 30)
        await asyncio.sleep(0.9)

    async def screen(x, y):
        return await ff.eval(f"{TO_SCREEN}({x}, {y})")

    # a new layer through the "Into" picker
    r = await ff.eval("(() => { const b = document.querySelector('[aria-label=\"Layer for new objects\"]').getBoundingClientRect();"
                      " return [Math.round(b.x + b.width / 2), Math.round(b.y + b.height / 2)]; })()")
    await click_at(ff, *r)
    await asyncio.sleep(0.3)
    await ff.eval("[...document.querySelectorAll('[role=option]')].find(o => o.textContent.includes('New layer')).click(), 1")
    await asyncio.sleep(0.3)
    if await ff.eval("!!document.querySelector('[role=dialog] [aria-label=Store]')"):
        await ff.eval(f"{SET_VALUE}(document.querySelector('[role=dialog] [aria-label=Store]'), '{store}')")
    name = f"smoke {int(time.time())}"
    await ff.eval(f"{SET_VALUE}(document.querySelector('[aria-label=\"Layer name\"]'), '{name}')")
    await ff.eval(f"{SET_VALUE}(document.querySelector('[aria-label=\"Group names\"]'), 'alpha, beta')")
    await ff.eval("[...document.querySelectorAll('[role=dialog] button')].find(b => b.textContent === 'Create').click(), 1")
    await settle_edit()
    tgt = await ff.eval("window.__cs.ann.layers.find(l => l.info.id === window.__cs.edit.target?.layerId)?.info.name")
    check(tgt == name, f"new layer created and chosen for drawing: {tgt}")

    # polygon by clicks (inspector open first: it narrows the canvas)
    if not await ff.eval("!!document.querySelector('aside[aria-label=Inspector]')"):
        await ff.key("]")
    await ff.key("1")
    await asyncio.sleep(1)
    cx, cy = await ff.eval("(() => { const r = window.__cs.viewer.deck.getCanvas().getBoundingClientRect();"
                           " return [Math.round(r.left + r.width / 2), Math.round(r.top + r.height / 2)]; })()")
    await ff.key("p")
    for dx, dy in [(-120, -100), (-20, -100), (0, -20), (-100, 0)]:
        await click_at(ff, cx + dx, cy + dy)
        await asyncio.sleep(0.15)
    await ff.key(ENTER)
    await settle_edit()
    n1 = await ff.eval(overlay)
    insp = await ff.eval(INSPECTOR)
    check(n1 == 1 and "polygon" in insp and name in insp, f"polygon drawn, saved and selected ({n1} new)")
    if shots:
        await ff.screenshot(shots / "8-drawn.png")
    await ff.chord(CTRL, "z")
    await settle_edit()
    n_undo = await ff.eval(overlay)
    await ff.chord(CTRL, SHIFT, "z")
    await settle_edit()
    check(n_undo == 0 and await ff.eval(overlay) == 1, "Ctrl+Z undoes, Ctrl+Shift+Z redoes")

    # vertex editing and moving
    await ff.key("v")
    await click_at(ff, cx - 60, cy - 50)
    await asyncio.sleep(0.5)
    await ff.key("e")
    await ff.wait_for("!!window.__cs.edit.checkout", 10)
    ring = json.loads(await ff.eval(CHECKOUT))[0]
    v0 = await screen(*ring[0])
    await drag(ff, v0, (v0[0] - 40, v0[1] - 40))
    await settle_edit()
    ring2 = json.loads(await ff.eval(CHECKOUT))[0]
    check(ring2[0] != ring[0] and ring2[1] == ring[1], f"dragging a vertex moves it: {ring[0]} -> {ring2[0]}")
    c = await screen(sum(p[0] for p in ring2[:4]) / 4, sum(p[1] for p in ring2[:4]) / 4)
    await drag(ff, c, (c[0] + 50, c[1]))
    await settle_edit()
    ring3 = json.loads(await ff.eval(CHECKOUT))[0]
    check(all(abs(b[0] - a[0] - 50) < 2 for a, b in zip(ring2, ring3)), "dragging inside moves the whole polygon")
    m = await screen((ring3[1][0] + ring3[2][0]) / 2, (ring3[1][1] + ring3[2][1]) / 2)
    await ff.mouse([{"type": "pointerMove", "x": m[0], "y": m[1] - 1}, {"type": "pointerMove", "x": m[0], "y": m[1], "duration": 50},
                    {"type": "pause", "duration": 100}, {"type": "pointerDown", "button": 0}, {"type": "pointerUp", "button": 0}])
    await settle_edit()
    n_v = len(json.loads(await ff.eval(CHECKOUT))[0]) - 1
    v1 = await screen(*json.loads(await ff.eval(CHECKOUT))[0][1])
    await click_at(ff, *v1)
    await settle_edit()
    n_plain = len(json.loads(await ff.eval(CHECKOUT))[0]) - 1
    await ff.send("input.performActions", {"context": ff.context, "actions": [
        {"type": "key", "id": "kbd", "actions": [{"type": "keyDown", "value": ALT}, {"type": "pause"}, {"type": "pause"},
                                                 {"type": "pause"}, {"type": "keyUp", "value": ALT}]},
        {"type": "pointer", "id": "mouse", "parameters": {"pointerType": "mouse"}, "actions": [
            {"type": "pause"}, {"type": "pointerMove", "x": v1[0], "y": v1[1]}, {"type": "pointerDown", "button": 0},
            {"type": "pointerUp", "button": 0}, {"type": "pause"}]}]})
    await settle_edit()
    n_alt = len(json.loads(await ff.eval(CHECKOUT))[0]) - 1
    check((n_v, n_plain, n_alt) == (5, 5, 4), f"edge click adds a vertex, plain click keeps it, Alt+click removes it ({n_v}, {n_plain}, {n_alt})")

    # group and attributes in the inspector
    await ff.eval(f"{SET_VALUE}(document.querySelector('aside[aria-label=Inspector] select[aria-label=Group]'), '1')")
    await settle_edit()
    check(await ff.eval("window.__cs.edit.checkout?.features[0].group") == 1, "inspector changes the group")
    await ff.eval(f"{SET_VALUE}(document.querySelector('[aria-label=\"Add attribute name\"]'), 'smoke_score')")
    await ff.eval(f"{SET_VALUE}(document.querySelector('[aria-label=\"Add attribute value\"]'), '0.75')")
    await ff.eval("document.querySelector('button[aria-label=\"Add attribute\"]').click(), 1")
    await asyncio.sleep(0.5)
    val = await ff.wait_for("document.querySelector('aside[aria-label=Inspector] input[aria-label=smoke_score]')?.value ?? null", 10)
    check(val == "0.75", f"inspector adds an attribute: smoke_score = {val}")
    await ff.key(DEL)
    await settle_edit()
    gone = await ff.eval(overlay)
    await ff.chord(CTRL, "z")
    await settle_edit()
    check(gone == 0 and await ff.eval(overlay) == 1, "Delete removes the selection; undo restores it")
    await ff.key(ESC)

    # the other tools
    import math
    tools = [("r", "polygon"), ("c", "polygon"), ("d", "polygon"), ("x", "point"), ("n", "polyline")]
    for i, (key, kind) in enumerate(tools):
        before = await ff.eval(overlay)
        await ff.key(key)
        await asyncio.sleep(0.2)
        x0, y0 = cx - 250 + 120 * i, cy + 80
        if key in "rc":
            await drag(ff, (x0, y0), (x0 + 80, y0 + 60))
        elif key == "d":
            pts = [(round(x0 + 40 + 40 * math.cos(t * math.pi / 10)), round(y0 + 30 + 30 * math.sin(t * math.pi / 10))) for t in range(21)]
            await ff.mouse([{"type": "pointerMove", "x": pts[0][0], "y": pts[0][1]}, {"type": "pointerDown", "button": 0},
                            *[{"type": "pointerMove", "x": x, "y": y, "duration": 20} for x, y in pts[1:]],
                            {"type": "pointerUp", "button": 0}])
        elif key == "x":
            await click_at(ff, x0 + 40, y0 + 30)
        else:
            for x, y in [(x0, y0), (x0 + 50, y0 + 40), (x0 + 90, y0)]:
                await click_at(ff, x, y)
                await asyncio.sleep(0.15)
            await ff.key(ENTER)
        await settle_edit()
        insp = await ff.eval(INSPECTOR)
        check(await ff.eval(overlay) == before + 1 and f"Type{kind}" in insp.replace("\n", "").replace("\t", ""),
              f"tool {key.upper()} draws a {kind}")
    if shots:
        await ff.screenshot(shots / "9-tools.png")
    await ff.key(ESC)

    # a commit by another client reaches the view through the event stream
    before = await ff.eval(overlay)
    status = await ff.eval(f"""(async () => {{
      const h = {{Authorization: 'Bearer {token}', 'Content-Type': 'application/json'}};
      const rev = (await (await fetch('api/v1/stores/{store}/revision', {{headers: h}})).json()).revision;
      const l = window.__cs.edit.targetInfo;
      const r = await fetch('api/v1/stores/{store}/commit', {{method: 'POST', headers: h, body: JSON.stringify({{
        base_revision: rev, client: 'smoke-other', ops: [{{op: 'create', group_id: l.groups[1].id,
        geometry: {{type: 'Point', coordinates: [window.__cs.viewer.view().x, window.__cs.viewer.view().y]}}}}]}})}});
      return r.status; }})()""")
    await asyncio.sleep(1.5)
    check(status == 200 and await ff.eval(overlay) == before + 1, "another client's commit appears without a reload")

    # first edit of a read-only layer: an editable copy takes its place, then Save / Discard edits
    async def inside_of(layer):
        """A level-0 point inside a polygon of `layer` (centroid of its largest triangle)."""
        await ff.wait_for(f"window.__cs.ann.sources.current.get('{layer}')?.polygons?.stats.pending === 0", 30)
        await asyncio.sleep(0.5)
        return await ff.eval(f"""(() => {{ const s = window.__cs.ann.sources.current.get('{layer}').polygons.shapes[0];
          if (!s) return null; const P = s.positions, I = s.indices; let best = 0, at = 0;
          for (let k = 0; k < I.length; k += 3) {{ const [a, b, c] = [I[k], I[k + 1], I[k + 2]];
            const A = Math.abs((P[2*b] - P[2*a]) * (P[2*c+1] - P[2*a+1]) - (P[2*c] - P[2*a]) * (P[2*b+1] - P[2*a+1]));
            if (A > best) {{ best = A; at = k; }} }}
          return [0, 1, 2].map(j => I[at + j]).reduce((o, i) => [o[0] + P[2 * i] / 3, o[1] + P[2 * i + 1] / 3], [0, 0]); }})()""")

    def drawn_polys(layer):
        """Polygons drawn for a layer: bulk ones (edited ones hidden) plus the edit overlay's."""
        return (f"window.__cs.ann.sources.current.get('{layer}').polygons.stats.polygons + "
                f"window.__cs.edit.overlayEntries.filter(e => e.source.layerId === '{layer}').reduce((a, e) => a + e.source.count, 0)")

    def card(layer, what):
        return f"document.querySelector('[data-layer=\"{layer}\"]')?.{what}"

    small = (f"window.__cs.ann.layers.filter(l => l.info.store_id === '{store}' && l.info.counts.polygon > 0 && l.info.counts.polygon < 10000"
             " && (COND)).sort((a, b) => a.info.counts.polygon - b.info.counts.polygon)[0]?.info.id ?? null")
    ro = await ff.eval(small.replace("COND", "!l.info.editable"))
    await ff.key("0")
    await asyncio.sleep(2)
    fork = None
    if ro:
        await ff.eval(f"[...document.querySelectorAll('[data-layer]')].filter(c => c.dataset.layer !== '{ro}')"
                      ".forEach(c => c.querySelector('button[aria-label=\"Hide layer\"]')?.click()), 1")
        inside = await inside_of(ro)
        ro_name = await ff.eval(f"window.__cs.ann.layers.find(l => l.info.id === '{ro}').info.name")
        n_ro = await ff.eval(f"window.__cs.ann.sources.current.get('{ro}').polygons.stats.polygons")
        await click_at(ff, *(await screen(*inside)))
        await asyncio.sleep(0.5)
        tgt = json.loads(await ff.eval("JSON.stringify(window.__cs.edit.target)"))
        grp = await ff.eval("+document.querySelector('aside[aria-label=Inspector] select[aria-label=Group]')?.value")
        check(tgt["layerId"] == ro and tgt["group"] == grp, f"'Into' follows the selection: {tgt}")
        await ff.key(DEL)
        dlg = await ff.wait_for("document.querySelector('[role=dialog][aria-label=\"Edit a read-only layer\"]')?.innerText ?? null", 10)
        check("editable copy" in dlg, "deleting from a read-only layer asks to make an editable copy")
        await ff.eval("[...document.querySelectorAll('[role=dialog] button')].find(b => b.textContent === 'Make an editable copy').click(), 1")
        await ff.wait_for(f"!window.__cs.ann.layers.some(l => l.info.id === '{ro}')", 60)
        fork = await ff.eval(f"window.__cs.ann.layers.find(l => l.info.name === '{ro_name}' && l.info.editable)?.info.id ?? null")
        await ff.wait_for(f"(+{card(fork, 'dataset.unsaved')} || 0) > 0", 15)
        await ff.wait_for(f"window.__cs.ann.sources.current.get('{fork}')?.polygons?.stats.pending === 0 && "
                          f"window.__cs.ann.sources.current.get('{fork}').polygons.stats.rebuilds > 0", 30)
        await asyncio.sleep(1)
        title = await ff.eval(card(fork, "querySelector('span.font-medium').textContent"))
        n_fork = await ff.eval(drawn_polys(fork))
        check(title == f"{ro_name} (editable)" and n_fork == n_ro - 1,
              f"the copy, '{title}', replaces the original in the list and the edit applies to it ({n_ro} -> {n_fork} polygons)")
        if shots:
            await ff.screenshot(shots / "10-editable.png")
    else:
        fork = await ff.eval(small.replace("COND", "!!l.info.forked_from"))
        print(f"SKIP fork (every small polygon layer was copied by an earlier run; using {fork})")
    if fork:
        name = await ff.eval(f"window.__cs.ann.layers.find(l => l.info.id === '{fork}').info.name")
        if await ff.eval(f"(+{card(fork, 'dataset.unsaved')} || 0) > 0"):
            await ff.eval(card(fork, "querySelector('button[title^=\"Make the edited\"]').click()") + ", 1")
            await ff.wait_for(f"{card(fork, 'dataset.unsaved')} === '0'", 15)
        check(await ff.eval(card(fork, "querySelector('span.font-medium').textContent")) == name and
              not await ff.eval(card(fork, "className.includes('bg-editing')")), f"'Save edits' leaves one layer, '{name}'")
        await ff.eval(f"[...document.querySelectorAll('[data-layer]')].filter(c => c.dataset.layer !== '{fork}')"
                      ".forEach(c => c.querySelector('button[aria-label=\"Hide layer\"]')?.click()), 1")
        await ff.eval(card(fork, "querySelector('button[aria-label=\"Show layer\"]')?.click()") + ", 1")
        inside = await inside_of(fork)
        n0 = await ff.eval(drawn_polys(fork))
        await click_at(ff, *(await screen(*inside)))
        await asyncio.sleep(0.5)
        await ff.key(DEL)
        await ff.wait_for(f"(+{card(fork, 'dataset.unsaved')} || 0) > 0", 15)
        await asyncio.sleep(0.5)
        n1 = await ff.eval(drawn_polys(fork))
        await ff.eval(card(fork, "querySelector('button[title^=\"Revert the layer\"]').click()") + ", 1")
        await asyncio.sleep(0.3)
        await ff.eval("[...document.querySelectorAll('[role=dialog] button')].find(b => b.textContent === 'Discard').click(), 1")
        await ff.wait_for(f"{card(fork, 'dataset.unsaved')} === '0'", 15)
        await asyncio.sleep(1)
        n2 = await ff.eval(drawn_polys(fork))
        check(n1 == n0 - 1 and n2 == n0, f"'Discard edits' reverts to the save point ({n0} -> {n1} -> {n2} polygons)")

    t = json.loads(await ff.eval("JSON.stringify(window.__cs.edit.timing.current)"))
    # reload: the undo stack comes back from the server log
    await ff.goto(f"{base}/?debug#token={token}")
    await ff.wait_for("!!document.querySelector('[role=toolbar][aria-label=\"Edit tools\"]') && !document.querySelector('[aria-label=\"Loading\"]')", 60)
    await ff.wait_for("window.__cs.edit.canUndo", 15)
    await ff.chord(CTRL, "z")
    await asyncio.sleep(1.5)
    check(await ff.eval(TOAST) == "Undone", "undo works after a reload")

    # "Discard changes": reverts this session's edits only, in one step that Redo brings back
    discard = "[...document.querySelectorAll('[role=toolbar] button')].find(b => b.textContent === 'Discard changes')"
    check(await ff.eval(f"{discard}.disabled"), "nothing to discard right after opening")
    before = await ff.eval(overlay)
    cx, cy = await ff.eval("(() => { const r = window.__cs.viewer.deck.getCanvas().getBoundingClientRect();"
                           " return [Math.round(r.left + r.width / 2), Math.round(r.top + r.height / 2)]; })()")
    await ff.key("x")
    for dx in (0, 40):
        await click_at(ff, cx + dx, cy)
        await settle_edit()
    await ff.key(ESC)
    drawn = await ff.eval(overlay)
    await ff.eval(f"{discard}.click(), 1")
    await asyncio.sleep(0.3)
    await ff.eval("[...document.querySelectorAll('[role=dialog] button')].find(b => b.textContent === 'Discard').click(), 1")
    await settle_edit()
    after = await ff.eval(overlay)
    toast = await ff.eval(TOAST)
    await ff.chord(CTRL, SHIFT, "z")
    await settle_edit()
    check(drawn == before + 2 and after == before and await ff.eval(overlay) == drawn and toast == "Discarded 2 changes",
          f"Discard changes reverts this session's edits; Redo restores them ({before} -> {drawn} -> {after} -> {await ff.eval(overlay)})")
    return t


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
    ap.add_argument("--edit", action="store_true",
                    help="also test editing (writes to the curation databases: run the server with a scratch --sidecar-dir)")
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

        imgs = await ff.eval("fetch('api/v1/images', {headers: {Authorization: 'Bearer " + a.token + "'}})"
                             ".then(r => r.json()).then(j => JSON.stringify(j))")
        multi = [i for i in json.loads(imgs) if i.get("display") == "channels"]
        if multi:
            await check_channels(ff, check, a.shots, a.base, a.token, multi[0])
            big = next((i for i in multi if len(i["channels"]) > 8), None)
            if big:
                await check_channel_limit(ff, check, a.base, a.token, big)
            else:
                print("SKIP channel limit (no image with more than 8 channels)")
        else:
            print("SKIP channels (no multichannel image served)")

        if a.edit:
            if polys >= 100 and await ff.eval("(async () => (await (await fetch('api/v1/stores', {headers: {Authorization: 'Bearer "
                                              + a.token + "'}})).json()).some(s => s.editable))()"):
                t = await check_editing(ff, check, a.shots, a.base, a.token, v0["image"])
                for k, v in t.items():
                    v = sorted(v)
                    if v:
                        print(f"     {k}: n={len(v)} p50 {v[len(v) // 2]:.0f} ms, max {v[-1]:.0f} ms")
            else:
                check(False, "editing needs a polygon layer of 100 or more on an editable store")
        else:
            print("SKIP editing (needs --edit and a scratch --sidecar-dir)")

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
