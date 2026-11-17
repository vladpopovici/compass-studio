"""Multichannel images (milestone 8): display detection, channel tiles, the
decoded-tile cache, histograms, display defaults, the synthetic writer, CLI."""

import gzip
import json

import numpy as np
import pytest
import zarr
from fastapi.testclient import TestClient

from compass_studio.app import create_app
from compass_studio.imaging.channels import SyntheticChannels, load_channel_defaults, synth_region
from compass_studio.imaging.pyramid import ZarrPyramid, display_of
from compass_studio.serve import main as serve_main
from compass_studio.synth.channels import write as write_channels
from conftest import HOST, TOKEN


def _client(images):
    c = TestClient(create_app(images, TOKEN, {HOST}, {f"http://{HOST}"}), base_url=f"http://{HOST}")
    c.headers["Authorization"] = f"Bearer {TOKEN}"
    return c


def _zarr(path, data, names):
    g = zarr.open_group(str(path), mode="w", zarr_format=3)
    g.attrs.update({"level_count": 1, "base_mpp": 0.5, "channel_names": names})
    a = g.create_array("0", shape=data.shape, dtype=data.dtype, chunks=(512, 512) + data.shape[2:],
                       shards=(1024, 1024) + data.shape[2:])
    a[:] = data
    return path


def test_display_of():
    assert display_of("uint8", ["R", "G", "B"], 3) == "rgb"
    assert display_of("uint8", [], 3) == "rgb"
    assert display_of("uint8", ["DAPI", "CD3", "CD8"], 3) == "channels"
    assert display_of("uint16", ["R", "G", "B"], 3) == "channels"
    assert display_of("uint8", ["a"] * 5, 5) == "channels"


def test_channel_defaults(tmp_path):
    f = tmp_path / "s.channels.json"
    names = ["DAPI", "CD3", "CD8"]
    f.write_text(json.dumps({"channels": [{"name": "CD8", "color": "#FF0000", "max": 900},
                                          {"index": 0, "visible": True, "min": 10, "max": 20}]}))
    d = load_channel_defaults(f, names, 65535)
    assert d[0] == {"color": None, "min": 10, "max": 20, "visible": True}
    assert d[1] == {"color": None, "min": None, "max": None, "visible": None}
    assert d[2] == {"color": "#ff0000", "min": None, "max": 900, "visible": None}
    for bad in ({"channels": [{"name": "nope"}]}, {"channels": [{"index": 3}]},
                {"channels": [{"name": "CD3", "color": "red"}]}, {"channels": [{"name": "CD3", "max": 70000}]},
                {"channels": [{"name": "CD3", "min": 5, "max": 5}]}, {"channels": [{"name": "CD3", "visible": 1}]},
                {"nope": []}):
        f.write_text(json.dumps(bad))
        with pytest.raises(ValueError):
            load_channel_defaults(f, names, 65535)


def test_channel_tiles_cache_and_histogram(tmp_path):
    rng = np.random.default_rng(1)
    data = rng.integers(0, 4000, size=(700, 900, 3), dtype=np.uint16)
    p = ZarrPyramid(_zarr(tmp_path / "mc.zarr", data, ["DAPI", "CD3", "CD8"]))
    assert p.role == "image" and p.display == "channels" and p.dtype == "uint16"
    with _client({"mc": p}) as c:
        info = c.get("/api/v1/images/mc").json()
        assert info["display"] == "channels" and info["channels"] == ["DAPI", "CD3", "CD8"]
        assert info["channel_defaults"] is None
        for ch in range(3):
            r = c.get(f"/api/v1/images/mc/tiles/0/1/0.bin?channel={ch}", headers={"Accept-Encoding": "gzip"})
            assert r.status_code == 200 and r.headers["content-encoding"] == "gzip"
            got = np.frombuffer(r.content, "<u2").reshape(512, 388)  # TestClient undoes the gzip
            np.testing.assert_array_equal(got, data[:512, 512:, ch])
        tiles = c.app.state.tiles
        assert tiles.misses == 1 and tiles.hits == 2  # one read for the three channels
        assert c.get("/api/v1/images/mc/tiles/0/0/0.bin").status_code == 400  # no channel
        assert c.get("/api/v1/images/mc/tiles/0/0/0.bin?channel=3").status_code == 400
        assert c.get("/api/v1/images/mc/tiles/0/0/0.jpg?channel=0").status_code == 400
        h = c.get("/api/v1/images/mc/histogram").json()
        assert h["level"] == 0 and len(h["channels"]) == 3
        for ch, e in enumerate(h["channels"]):
            assert len(e["counts"]) == 256 and sum(e["counts"]) == 700 * 900
            assert e["min"] == data[..., ch].min() and e["max"] == data[..., ch].max()
            assert abs(e["percentiles"]["50"] - np.percentile(data[..., ch], 50)) < 1e-6
            assert set(e["percentiles"]) >= {"0.1", "1", "99", "99.9"}


def test_rgb_images_unchanged(tmp_path):
    p = ZarrPyramid(_zarr(tmp_path / "rgb.zarr", np.full((600, 600, 3), 9, np.uint8), ["R", "G", "B"]))
    with _client({"rgb": p}) as c:
        assert c.get("/api/v1/images/rgb").json()["display"] == "rgb"
        assert c.get("/api/v1/images/rgb/tiles/0/0/0.jpg").status_code == 200
        assert c.get("/api/v1/images/rgb/tiles/0/0/0.jpg?channel=0").status_code == 400
        assert c.get("/api/v1/images/rgb/histogram").status_code == 400


def test_synthetic_channels():
    p = SyntheticChannels(5000, 4000, 4)
    assert p.channels == ["DAPI", "Marker 1", "Marker 2", "Marker 3"] and p.display == "channels"
    a = p.read_tile(0, 2, 1)
    assert a.shape == (512, 512, 4) and a.dtype == np.uint16
    assert a[..., 0].max() > 3000 and a[..., 0].min() < 400  # nuclei and background
    np.testing.assert_array_equal(a, synth_region(512, 1024, 512, 512, 1, 4, (5000, 4000)))


def test_synth_writer_and_cli(tmp_path, monkeypatch):
    out = tmp_path / "s.zarr"
    write_channels(out, 1300, 900, 3, log=lambda m: None)
    p = ZarrPyramid(out)
    assert p.display == "channels" and p.channels == ["DAPI", "Marker 1", "Marker 2"] and len(p.levels) == 3
    assert all(p.fast_levels)
    np.testing.assert_array_equal(p.read_tile(0, 1, 1), synth_region(512, 512, 388, 512, 1, 3, (1300, 900)))
    (tmp_path / "s.channels.json").write_text(json.dumps({"channels": [{"name": "DAPI", "color": "#0000ff"}]}))
    # a single integer channel without a paired image is a one-channel image, not a label map
    g = zarr.open_group(str(tmp_path / "gray.zarr"), mode="w", zarr_format=3)
    g.attrs.update({"level_count": 1, "base_mpp": 0.5})
    g.create_array("0", shape=(600, 600), dtype="uint16", chunks=(512, 512), shards=(1024, 1024))[:] = 7
    seen = {}
    monkeypatch.setattr("compass_studio.serve.uvicorn.run", lambda app, **kw: seen.setdefault("app", app))
    serve_main(["--images", str(tmp_path), "--synthetic-channels", "syn=3000x2000x5"])
    c = TestClient(seen["app"], base_url=f"http://{HOST}")
    tok = next(m.kwargs["token"] for m in seen["app"].user_middleware if "token" in m.kwargs)
    c.headers["Authorization"] = f"Bearer {tok}"
    imgs = {i["id"]: i for i in c.get("/api/v1/images").json()}
    assert imgs["s"]["channel_defaults"][0]["color"] == "#0000ff"
    assert imgs["gray"]["role"] == "image" and imgs["gray"]["display"] == "channels" and imgs["gray"]["channels"] == ["Channel 1"]
    raw = c.get("/api/v1/images/gray/tiles/0/0/0.bin?channel=0").content
    assert np.frombuffer(raw, "<u2").max() == 7
    assert imgs["syn"]["channels"][0] == "DAPI" and len(imgs["syn"]["channels"]) == 5
    assert len(gzip.compress(raw)) < len(raw)
