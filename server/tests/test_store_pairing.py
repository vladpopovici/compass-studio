"""Annotation stores paired with images (contract 1.3.0 Store.aligned_to)."""

import shutil

import pytest
from fastapi.testclient import TestClient

from compass_studio.serve import main as serve_main
from test_labels import _rgb


def _serve(monkeypatch, argv) -> TestClient:
    seen = {}
    monkeypatch.setattr("compass_studio.serve.uvicorn.run", lambda app, **kw: seen.setdefault("app", app))
    serve_main(argv)
    c = TestClient(seen["app"], base_url="http://127.0.0.1:8765")
    tok = None
    for m in seen["app"].user_middleware:
        tok = m.kwargs.get("token", tok)
    c.headers["Authorization"] = f"Bearer {tok}"
    return c


def _aligned(c) -> dict:
    return {s["id"]: s["aligned_to"] for s in c.get("/api/v1/stores").json()}


def test_by_name(synth_store, monkeypatch):
    # the store is 4096 x 2048
    c = _serve(monkeypatch, ["--synthetic", "t=4096x2048", "--synthetic", "u=4096x2048",
                             "--annotations", f"t.cells={synth_store}", "--read-only"])
    assert _aligned(c) == {"t.cells": "t"}


def test_only_image(synth_store, monkeypatch):
    c = _serve(monkeypatch, ["--synthetic", "a=4096x2048", "--annotations", f"x={synth_store}", "--read-only"])
    assert _aligned(c) == {"x": "a"}


def test_guess_with_other_size_is_unpaired(synth_store, monkeypatch, capsys):
    c = _serve(monkeypatch, ["--synthetic", "a=100x100", "--annotations", f"x={synth_store}", "--read-only"])
    assert _aligned(c) == {"x": None}
    assert "not shown" in capsys.readouterr().out


def test_no_images(synth_store, monkeypatch):
    c = _serve(monkeypatch, ["--annotations", f"x={synth_store}", "--read-only"])
    assert _aligned(c) == {"x": None}


def test_explicit(synth_store, monkeypatch):
    c = _serve(monkeypatch, ["--synthetic", "a=4096x2048", "--synthetic", "b=4096x2048",
                             "--annotations", f"a.x={synth_store}", "--annotations-of", "a.x=b", "--read-only"])
    assert _aligned(c) == {"a.x": "b"}


@pytest.mark.parametrize("argv, err", [
    (["--synthetic", "t=100x100", "--annotations", "t.cells={s}"], "pixel-aligned"),
    (["--synthetic", "a=4096x2048", "--annotations", "x={s}", "--annotations-of", "x=b"], "no image"),
    (["--synthetic", "a=4096x2048", "--annotations", "x={s}", "--annotations-of", "y=a"], "no such annotation store"),
])
def test_errors(synth_store, argv, err, capsys):
    with pytest.raises(SystemExit):
        serve_main([x.format(s=synth_store) for x in argv] + ["--read-only"])
    assert err in capsys.readouterr().err


def test_folder_pickup(tmp_path, synth_store, monkeypatch):
    _rgb(tmp_path / "s.zarr", shape=(2048, 4096))
    shutil.copy(synth_store, tmp_path / "s.cells.ann.sqlite")
    c = _serve(monkeypatch, ["--images", str(tmp_path), "--read-only"])
    assert _aligned(c) == {"s.cells": "s"}
    assert {l["store_id"] for l in c.get("/api/v1/layers").json()} == {"s.cells"}
