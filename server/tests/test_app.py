"""v1 server: security, images, annotations, editing, events, spike endpoints."""

import io
import json
import queue
import threading

import numpy as np
import pyarrow as pa
from fastapi.testclient import TestClient
from PIL import Image

from compass_studio.annot.sidecar import NEW_ID_BASE
from compass_studio.app import create_app
from compass_studio.imaging.pyramid import SyntheticPyramid
from conftest import HOST, TOKEN

SQ = {"type": "Polygon", "coordinates": [[[100, 100], [140, 100], [140, 150], [100, 150], [100, 100]]]}


def arrow(r):
    assert r.status_code == 200 and r.headers["content-type"] == "application/vnd.apache.arrow.stream"
    return pa.ipc.open_stream(r.content).read_all()


def test_security(client):
    assert client.get("/api/v1/info").status_code == 200
    assert client.get("/api/v1/info", headers={"Authorization": "Bearer x"}).status_code == 401
    assert client.get("/api/v1/info", headers={"Host": "evil.example:8765"}).status_code == 421
    assert client.get("/api/v1/info", headers={"Origin": "http://evil.example"}).status_code == 403
    assert client.get("/api/v1/info", headers={"Origin": f"http://{HOST}"}).status_code == 200


def test_info(client):
    i = client.get("/api/v1/info").json()
    assert i["api_version"] == "1.3.0" and "edit" in i["capabilities"]
    assert i["schemas"] == {"point-layer": ["1"], "polygon-tile": ["1"], "centroid-tile": ["1"]}


def test_image_tiles(client):
    info = client.get("/api/v1/images/img").json()
    assert [lv["width"] for lv in info["levels"]] == [3000, 1500, 750, 375]
    im = np.asarray(Image.open(io.BytesIO(client.get("/api/v1/images/img/tiles/0/5/2.png").content)))
    assert im.shape == (1200 - 2 * 512, 3000 - 5 * 512, 3)
    assert client.get("/api/v1/images/img/tiles/0/6/0.jpg").status_code == 404
    assert client.get("/api/v1/images/img/tiles/9/0/0.jpg").status_code == 404
    assert client.get("/api/v1/images/img/tiles/0/0/0.gif").status_code == 400
    r = client.get("/api/v1/images/img/tiles/0/0/0.webp")
    assert r.headers["content-type"] == "image/webp"
    im = np.asarray(Image.open(io.BytesIO(r.content)).convert("RGB"))
    np.testing.assert_array_equal(im, SyntheticPyramid(3000, 1200).read_tile(0, 0, 0))  # lossless


def test_tile_caching(client):
    info = client.get("/api/v1/images/img").json()
    ver = info["version"]
    assert ver and info["name"] and info["objective_power"] is None
    r = client.get("/api/v1/images/img/tiles/0/0/0.jpg")
    assert r.headers["cache-control"] == "private, no-cache" and r.headers["etag"] == f'"{ver}"'
    r = client.get(f"/api/v1/images/img/tiles/0/0/0.jpg?v={ver}")
    assert r.headers["cache-control"] == "private, max-age=31536000, immutable"
    assert client.get("/api/v1/images/img/tiles/0/0/0.jpg?v=old").headers["cache-control"] == "private, no-cache"
    r = client.get("/api/v1/images/img/tiles/0/0/0.jpg", headers={"If-None-Match": f'"{ver}"'})
    assert r.status_code == 304 and not r.content
    assert client.get("/api/v1/images/img/tiles/0/0/0.jpg", headers={"If-None-Match": '"x"'}).status_code == 200
    assert client.get("/api/v1/images/img/tiles/0/99/0.jpg",
                      headers={"If-None-Match": f'"{ver}"'}).status_code == 404


def test_layers_points_tiles(client):
    stores = client.get("/api/v1/stores").json()
    assert stores[0]["id"] == "synth" and stores[0]["editable"] and stores[0]["image_width"] == 4096
    layers = {lv["id"]: lv for lv in client.get("/api/v1/layers").json()}
    assert set(layers) == {"synth:1", "synth:2", "synth:3"}
    assert layers["synth:1"]["counts"] == {"point": 500} and layers["synth:2"]["counts"] == {"polygon": 400}
    assert [g["index"] for g in layers["synth:2"]["groups"]] == list(range(8))

    pts = arrow(client.get("/api/v1/layers/synth:1/points"))
    assert pts.num_rows == 500 and pts.schema.field("object_id").type == pa.int64()
    assert pts.schema.metadata[b"schema"] == b"point-layer" and max(pts["group"].to_pylist()) < 8

    # polygon tiles: every cell appears in at least one tile of the level-0 grid
    seen = set()
    for col in range(8):
        for row in range(4):
            t = arrow(client.get(f"/api/v1/layers/synth:2/tiles/0/{col}/{row}"))
            assert t.schema.metadata[b"schema"] == b"polygon-tile"
            seen |= set(t["object_id"].to_pylist())
    assert len(seen) == 400
    assert client.get("/api/v1/layers/synth:2/tiles/0/8/0").status_code == 404
    # above polygon_max_level: centroids, each object exactly once
    c = arrow(client.get("/api/v1/layers/synth:2/tiles/4/0/0"))
    assert c.schema.metadata[b"schema"] == b"centroid-tile" and c.num_rows == 400

    ids = ",".join(str(i) for i in sorted(seen)[:3])
    fc = client.get(f"/api/v1/layers/synth:2/features?ids={ids}").json()
    assert len(fc["features"]) == 3 and fc["features"][0]["geometry"]["type"] == "Polygon"
    objs = client.get(f"/api/v1/layers/synth:2/objects?ids={ids}").json()["objects"]
    assert len(objs) == 3 and all("values" in o for o in objs)
    assert client.get("/api/v1/layers/synth:9").status_code == 404
    assert client.get("/api/v1/layers/nope").status_code == 404
    assert client.get("/api/v1/layers/synth:2/features?ids=x").status_code == 400


def test_fork_edit_undo_conflict(client):
    rev0 = client.get("/api/v1/stores/synth/revision").json()["revision"]
    r = client.post("/api/v1/layers/synth:2/fork", json={"name": "cells curated"})
    assert r.status_code == 201
    layer = r.json()
    assert layer["id"].startswith("synth:edit:") and layer["editable"] and layer["forked_from"] == "synth:2"
    assert layer["counts"] == {"polygon": 400} and layer["revision"] == rev0 + 1
    g = layer["groups"][0]["id"]

    rev = layer["revision"]
    r = client.post("/api/v1/stores/synth/commit", json={"base_revision": rev, "client": "t", "ops": [
        {"op": "create", "group_id": g, "geometry": SQ, "values": {"curated": 1.0}}]})
    assert r.status_code == 200
    new_id = r.json()["created"][0]
    assert new_id >= NEW_ID_BASE and r.json()["revision"] == rev + 1
    t = arrow(client.get(f"/api/v1/layers/{layer['id']}/tiles/0/0/0"))
    assert new_id in t["object_id"].to_pylist()
    assert client.get(f"/api/v1/layers/{layer['id']}/objects?ids={new_id}").json()["objects"][0]["values"] == \
        {"curated": 1.0}

    stale = client.post("/api/v1/stores/synth/commit", json={"base_revision": rev, "ops": []})
    assert stale.status_code == 409 and stale.json()["current_revision"] == rev + 1
    bad = client.post("/api/v1/stores/synth/commit", json={"base_revision": rev + 1, "ops": [{"op": "nope"}]})
    assert bad.status_code == 422

    log = client.get(f"/api/v1/stores/synth/log?since={rev}").json()["entries"]
    assert [e["revision"] for e in log] == [rev + 1]
    undo = client.post("/api/v1/stores/synth/commit", json={"base_revision": rev + 1, "ops": log[0]["inverse"]})
    assert undo.status_code == 200
    t = arrow(client.get(f"/api/v1/layers/{layer['id']}/tiles/0/0/0"))
    assert new_id not in t["object_id"].to_pylist()

    # store layers stay read-only; sidecar layers cannot be forked
    assert client.post(f"/api/v1/layers/{layer['id']}/fork").status_code == 400
    r = client.post("/api/v1/stores/synth/layers", json={"name": "mine", "groups": ["a", "b"]})
    assert r.status_code == 201 and [x["name"] for x in r.json()["groups"]] == ["a", "b"]
    assert client.post("/api/v1/stores/synth/layers", json={"name": "x", "groups": ["a", "a"]}).status_code == 422


def test_events(client):
    events: queue.Queue = queue.Queue()

    def listen():
        with client.stream("GET", "/api/v1/events") as r:
            name = None
            for line in r.iter_lines():
                if line.startswith("event: "):
                    name = line[7:]
                elif line.startswith("data: "):
                    events.put((name, json.loads(line[6:])))
                    if name == "revision":
                        return

    th = threading.Thread(target=listen, daemon=True)
    th.start()
    name, hello = events.get(timeout=10)
    assert name == "hello" and "synth" in hello["revisions"]
    client.post("/api/v1/stores/synth/layers", json={"name": "ev", "groups": ["a"]})
    name, ev = events.get(timeout=10)
    assert name == "revision" and ev["store_id"] == "synth" and ev["revision"] == hello["revisions"]["synth"] + 1


def test_read_only_store(tmp_path, synth_store):
    from compass_studio.app import Dataset
    ds = Dataset.open("ro", synth_store, None)
    app = create_app({}, TOKEN, {HOST}, {f"http://{HOST}"}, datasets={"ro": ds})
    with TestClient(app, base_url=f"http://{HOST}") as c:
        c.headers["Authorization"] = f"Bearer {TOKEN}"
        assert c.get("/api/v1/stores/ro").json()["editable"] is False
        assert c.post("/api/v1/layers/ro:2/fork").status_code == 403
        assert c.get("/api/v1/stores/ro/revision").status_code == 403


def test_spike_endpoints_outside_v1(tmp_path):
    (tmp_path / "f.arrow").write_bytes(b"abc")
    app = create_app({}, TOKEN, {HOST}, {f"http://{HOST}"}, spike_files=tmp_path, results_dir=tmp_path / "r")
    c = TestClient(app, base_url=f"http://{HOST}")
    assert c.get("/api/spike/files/f.arrow").status_code == 401
    auth = {"Authorization": f"Bearer {TOKEN}"}
    r = c.get("/api/spike/files/f.arrow", headers=auth)
    assert r.status_code == 200 and r.content == b"abc"
    assert c.get("/api/spike/files/../pyproject.toml", headers=auth).status_code == 404
    assert c.post("/api/spike/results/s2-pan", json={"a": 1}, headers=auth).status_code == 200
    assert len(list((tmp_path / "r").glob("*-s2-pan.json"))) == 1
