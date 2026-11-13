"""Label maps: class tables, pairing, 2-D Zarr maps, bin/png tiles, CLI."""

import gzip
import io
import json

import numpy as np
import pytest
import zarr
from fastapi.testclient import TestClient
from PIL import Image

from compass_studio.app import create_app
from compass_studio.imaging.labels import SyntheticLabels, classes_file_for, load_classes, pair_labels
from compass_studio.imaging.pyramid import SyntheticPyramid, ZarrPyramid
from compass_studio.serve import main as serve_main
from conftest import HOST, TOKEN


def _map(path, shape=(1100, 1300), dtype="uint16"):
    """A COMPASS-style 2-D map: (y, x) arrays, nearest-neighbour levels."""
    rng = np.random.default_rng(0)
    base = rng.integers(0, 1000, size=shape).astype(dtype)
    g = zarr.open_group(str(path), mode="w", zarr_format=3)
    g.attrs.update({"level_count": 2, "base_mpp": 0.5, "channel_names": ["region"],
                    "dimension_names": ["y", "x"]})
    for i, a in enumerate((base, base[::2, ::2])):
        arr = g.create_array(str(i), shape=a.shape, dtype=dtype, chunks=(512, 512), shards=(1024, 1024))
        arr[:] = a
    return path, base


def _rgb(path, shape=(1100, 1300)):
    g = zarr.open_group(str(path), mode="w", zarr_format=3)
    g.attrs.update({"level_count": 1, "base_mpp": 0.5, "channel_names": ["R", "G", "B"]})
    a = g.create_array("0", shape=(*shape, 3), dtype="uint8", chunks=(512, 512, 3), shards=(512, 512, 3))
    a[:] = 200
    return path


def test_load_classes(tmp_path):
    f = tmp_path / "c.json"
    f.write_text(json.dumps({"background": 7, "classes": [
        {"value": 2, "name": "b", "color": "#ABCDEF"}, {"value": 1, "name": "a"}]}))
    assert load_classes(f) == ([{"value": 1, "name": "a", "color": None},
                                {"value": 2, "name": "b", "color": "#abcdef"}], 7)
    for bad in ({"classes": [{"value": 1, "name": "a"}, {"value": 1, "name": "b"}]},
                {"classes": [{"value": -1, "name": "a"}]}, {"classes": [{"value": 1, "name": ""}]},
                {"classes": [{"value": 1, "name": "a", "color": "red"}]}, {"background": "x"}):
        f.write_text(json.dumps(bad))
        with pytest.raises(ValueError):
            load_classes(f)


def test_zarr_map_and_pairing(tmp_path):
    p, base = _map(tmp_path / "s.regions.zarr")
    lab = ZarrPyramid(p)
    assert lab.role == "labels" and lab.dtype == "uint16" and lab.background == 0
    assert all(lab.fast_levels)
    np.testing.assert_array_equal(lab.read_tile(0, 2, 2), base[1024:, 1024:])
    np.testing.assert_array_equal(lab.read_tile(1, 1, 0), base[::2, ::2][:512, 512:])
    img = ZarrPyramid(_rgb(tmp_path / "s.zarr"))
    assert img.role == "image" and "aligned_to" not in img.info()
    pair_labels({"s.regions": lab}, {"s": img, "t": SyntheticPyramid(5, 5)})
    assert lab.aligned_to == "s"
    with pytest.raises(ValueError, match="pixel-aligned"):
        pair_labels({"x": lab}, {"t": SyntheticPyramid(5, 5)})
    with pytest.raises(ValueError, match="belong"):
        pair_labels({"x": lab}, {"s": img, "t": SyntheticPyramid(5, 5)})
    assert classes_file_for(p) is None
    (tmp_path / "s.regions.classes.json").write_text("{}")
    assert classes_file_for(p).name == "s.regions.classes.json"


def test_label_tiles(tmp_path):
    p, base = _map(tmp_path / "m.zarr")
    lab = ZarrPyramid(p)
    lab.aligned_to, lab.classes = "img", [{"value": 1, "name": "a", "color": None}]
    app = create_app({"img": SyntheticPyramid(1300, 1100), "lab": lab, "syn": SyntheticLabels(1300, 1100, dtype="uint32")},
                     TOKEN, {HOST}, {f"http://{HOST}"})
    c = TestClient(app, base_url=f"http://{HOST}")
    c.headers["Authorization"] = f"Bearer {TOKEN}"
    info = c.get("/api/v1/images/lab").json()
    assert info["role"] == "labels" and info["aligned_to"] == "img" and info["classes"][0]["name"] == "a"
    assert c.get("/api/v1/images/img").json()["role"] == "image"
    assert {"labels", "tiles.bin"} <= set(c.get("/api/v1/info").json()["capabilities"])

    # gzip-encoded raw values (httpx decodes transparently; check the header and the bytes)
    r = c.get("/api/v1/images/lab/tiles/0/2/0.bin", headers={"Accept-Encoding": "gzip"})
    assert r.headers["content-encoding"] == "gzip" and r.headers["vary"] == "Accept-Encoding"
    assert r.headers["etag"].endswith('-gz"')
    np.testing.assert_array_equal(np.frombuffer(r.content, "<u2").reshape(512, 1300 - 1024), base[:512, 1024:])
    r = c.get("/api/v1/images/lab/tiles/0/2/2.bin", headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in r.headers
    np.testing.assert_array_equal(np.frombuffer(r.content, "<u2").reshape(1100 - 1024, 1300 - 1024),
                                  base[1024:, 1024:])
    im = np.asarray(Image.open(io.BytesIO(c.get("/api/v1/images/lab/tiles/0/0/0.png").content)))
    np.testing.assert_array_equal(im, base[:512, :512])
    raw = c.get("/api/v1/images/syn/tiles/0/0/0.bin").content
    assert {0, 1, 2} <= set(np.unique(np.frombuffer(raw, "<u4"))) <= set(range(6))
    assert c.get("/api/v1/images/syn/tiles/0/0/0.png").status_code == 400  # 32-bit
    assert c.get("/api/v1/images/lab/tiles/0/0/0.jpg").status_code == 400
    assert c.get("/api/v1/images/img/tiles/0/0/0.bin").status_code == 400
    v = info["version"]
    assert c.get("/api/v1/images/lab/tiles/0/0/0.bin", headers={"If-None-Match": f'"{v}-gz"'}).status_code == 304
    assert len(gzip.compress(raw)) < len(raw)


def test_cli_folder(tmp_path, monkeypatch):
    _rgb(tmp_path / "s.zarr")
    _map(tmp_path / "s.regions.zarr")
    (tmp_path / "s.regions.classes.json").write_text(json.dumps({"classes": [{"value": 3, "name": "x"}]}))
    seen = {}
    monkeypatch.setattr("compass_studio.serve.uvicorn.run", lambda app, **kw: seen.setdefault("app", app))
    serve_main(["--images", str(tmp_path)])
    c = TestClient(seen["app"], base_url="http://127.0.0.1:8765")
    tok = None
    for m in seen["app"].user_middleware:
        tok = m.kwargs.get("token", tok)
    c.headers["Authorization"] = f"Bearer {tok}"
    imgs = {i["id"]: i for i in c.get("/api/v1/images").json()}
    assert imgs["s"]["role"] == "image"
    assert imgs["s.regions"]["aligned_to"] == "s" and imgs["s.regions"]["classes"][0]["value"] == 3


def test_cli_rejects_unpaired(tmp_path, capsys):
    _map(tmp_path / "m.zarr")
    with pytest.raises(SystemExit):
        serve_main(["--labels", f"m={tmp_path / 'm.zarr'}", "--synthetic", "a=10x10", "--synthetic", "b=10x10"])
    assert "belong" in capsys.readouterr().err
