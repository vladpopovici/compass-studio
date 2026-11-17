"""The one command: slides, folders and stores given as paths; the bundled client."""

import shutil

import pytest

from compass_studio import serve
from test_labels import _map, _rgb
from test_store_pairing import _aligned, _serve


@pytest.fixture
def slide_dir(tmp_path, synth_store):
    _rgb(tmp_path / "s.zarr", shape=(2048, 4096))
    _map(tmp_path / "s.tissue.zarr", shape=(2048, 4096))
    _rgb(tmp_path / "other.zarr")
    shutil.copy(synth_store, tmp_path / "s.cells.ann.sqlite")
    shutil.copy(synth_store, tmp_path / "other.ann.sqlite")
    return tmp_path


def test_slide_files(slide_dir):
    images, stores = serve.slide_files(slide_dir / "s.zarr")
    assert sorted(images) == ["s", "s.tissue"]
    assert sorted(stores) == ["s.cells"]


def test_slide_path(slide_dir, monkeypatch):
    c = _serve(monkeypatch, [str(slide_dir / "s.zarr"), "--read-only"])
    roles = {i["id"]: i["role"] for i in c.get("/api/v1/images").json()}
    assert roles == {"s": "image", "s.tissue": "labels"}
    assert _aligned(c) == {"s.cells": "s"}


def test_slide_and_its_folder(slide_dir, monkeypatch):
    # the same files reached twice are served once
    c = _serve(monkeypatch, [str(slide_dir / "s.zarr"), str(slide_dir), "--read-only"])
    assert sorted(i["id"] for i in c.get("/api/v1/images").json()) == ["other", "s", "s.tissue"]
    assert sorted(_aligned(c)) == ["other", "s.cells"]


def test_store_path(synth_store, monkeypatch):
    c = _serve(monkeypatch, ["--synthetic", "t=4096x2048", str(synth_store), "--read-only"])
    assert _aligned(c) == {"t": "t"}


@pytest.mark.parametrize("make, err", [
    (lambda d: d / "missing.zarr", "no such file"),
    (lambda d: (d / "x.txt").write_text("") or d / "x.txt", "expected a .zarr slide"),
    (lambda d: (d / "e.zarr").mkdir() or d / "e.zarr", "no zarr.json"),
])
def test_bad_paths(tmp_path, monkeypatch, capsys, make, err):
    monkeypatch.setattr("compass_studio.serve.uvicorn.run", lambda app, **kw: None)
    with pytest.raises(SystemExit):
        serve.main([str(make(tmp_path)), "--read-only"])
    assert err in capsys.readouterr().err


def test_port_busy(monkeypatch, capsys):
    monkeypatch.setattr("compass_studio.serve.port_busy", lambda host, port: "Address already in use")
    with pytest.raises(SystemExit):
        serve.main(["--synthetic", "a=100x100"])
    assert "choose another port" in capsys.readouterr().err


def test_web_default(monkeypatch, tmp_path):
    assert serve.bundled_web() is None or (serve.bundled_web() / "index.html").is_file()
    (tmp_path / "index.html").write_text("<!doctype html>")
    monkeypatch.setattr("compass_studio.serve.bundled_web", lambda: tmp_path)
    c = _serve(monkeypatch, ["--synthetic", "a=100x100"])
    assert c.get("/").text == "<!doctype html>"
    c = _serve(monkeypatch, ["--synthetic", "a=100x100", "--no-web"])
    assert c.get("/").status_code == 404
