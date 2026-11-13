"""Tile server: security checks and tile shapes."""

import io

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from compass_studio.app import create_app
from compass_studio.imaging.pyramid import SyntheticPyramid

TOKEN = "t0k3n"
HOST = "127.0.0.1:8765"


@pytest.fixture
def client(tmp_path):
    app = create_app({"s": SyntheticPyramid(3000, 1200)}, TOKEN, {HOST},
                     {f"http://{HOST}"}, results_dir=tmp_path)
    c = TestClient(app, base_url=f"http://{HOST}")
    c.headers["Authorization"] = f"Bearer {TOKEN}"
    return c


def test_security(client):
    assert client.get("/api/v1/info").status_code == 200
    assert client.get("/api/v1/info", headers={"Authorization": "Bearer x"}).status_code == 401
    assert client.get("/api/v1/info", headers={"Host": "evil.example:8765"}).status_code == 421
    assert client.get("/api/v1/info", headers={"Origin": "http://evil.example"}).status_code == 403
    assert client.get("/api/v1/info", headers={"Origin": f"http://{HOST}"}).status_code == 200


def test_levels_and_edge_tiles(client):
    info = client.get("/api/v1/images/s").json()
    assert [l["width"] for l in info["levels"]] == [3000, 1500, 750, 375]
    r = client.get("/api/v1/images/s/tiles/0/5/2.png")
    assert r.status_code == 200
    im = np.asarray(Image.open(io.BytesIO(r.content)))
    assert im.shape == (1200 - 2 * 512, 3000 - 5 * 512, 3)
    assert client.get("/api/v1/images/s/tiles/0/6/0.jpg").status_code == 404
    assert client.get("/api/v1/images/s/tiles/9/0/0.jpg").status_code == 404
    assert "read;dur=" in client.get("/api/v1/images/s/tiles/3/0/0.jpg").headers["server-timing"]
    r = client.get("/api/v1/images/s/tiles/0/0/0.webp")
    assert r.headers["content-type"] == "image/webp"
    im = np.asarray(Image.open(io.BytesIO(r.content)).convert("RGB"))
    from compass_studio.imaging.pyramid import SyntheticPyramid as SP
    np.testing.assert_array_equal(im, SP(3000, 1200).read_tile(0, 0, 0))  # lossless


def test_spike_files_need_token(tmp_path):
    (tmp_path / "f.arrow").write_bytes(b"abc")
    app = create_app({}, TOKEN, {HOST}, {f"http://{HOST}"}, spike_files=tmp_path)
    c = TestClient(app, base_url=f"http://{HOST}")
    assert c.get("/api/v1/spike/files/f.arrow").status_code == 401
    r = c.get("/api/v1/spike/files/f.arrow", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200 and r.content == b"abc"
    assert c.get("/api/v1/spike/files/../pyproject.toml",
                 headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 404


def test_results(client, tmp_path):
    assert client.post("/api/v1/spike/results/s2-pan", json={"a": 1}).status_code == 200
    assert client.post("/api/v1/spike/results/..%2Fx", json={}).status_code in (400, 404)
    assert len(list(tmp_path.glob("*-s2-pan.json"))) == 1


def test_edit_api(tmp_path):
    from compass_studio.annot.sidecar import Sidecar
    sc = Sidecar(tmp_path / "c.sqlite")
    _, (g,) = sc.create_layer("mine", ["a"])
    app = create_app({}, TOKEN, {HOST}, {f"http://{HOST}"}, sidecar=sc)
    c = TestClient(app, base_url=f"http://{HOST}")
    c.headers["Authorization"] = f"Bearer {TOKEN}"
    sq = [[[0, 0], [10, 0], [10, 10], [0, 10]]]
    r = c.post("/api/v1/edit/commit", json={"base_revision": 0, "ops": [
        {"op": "create", "group_id": g, "coords": sq}]})
    assert r.status_code == 200 and r.json()["revision"] == 1
    oid = r.json()["created"][0]
    fc = c.get(f"/api/v1/edit/objects?ids={oid}").json()
    assert fc["revision"] == 1 and fc["features"][0]["geometry"]["type"] == "Polygon"
    stale = c.post("/api/v1/edit/commit", json={"base_revision": 0, "ops": [{"op": "delete", "object_id": oid}]})
    assert stale.status_code == 409 and stale.json()["current_revision"] == 1
    assert c.post("/api/v1/edit/commit", json={"base_revision": 1, "ops": [{"op": "nope"}]}).status_code == 400
