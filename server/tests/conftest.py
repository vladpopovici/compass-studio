"""Shared fixtures: a v1 server over a synthetic image and a small synthetic
annotation store with its sidecar."""

import pytest
from fastapi.testclient import TestClient

from compass_studio.app import Dataset, create_app
from compass_studio.imaging.pyramid import SyntheticPyramid
from compass_studio.synth.generate import Params, generate

TOKEN = "t0k3n"
HOST = "127.0.0.1:8765"


@pytest.fixture(scope="session")
def synth_store(tmp_path_factory):
    d = tmp_path_factory.mktemp("store")
    generate(d, "t", Params(width=4096, height=2048, points=500, cells=400, regions=3,
                            region_vertices=(60, 120), region_radius_px=(100.0, 300.0), attrs=6, lines=4),
             arrow=False, log=lambda m: None)
    return d / "t.ann.sqlite"


def make_client(tmp_path, synth_store, **kw):
    ds = Dataset.open("synth", synth_store, tmp_path / "synth.curation.sqlite")
    app = create_app({"img": SyntheticPyramid(3000, 1200)}, TOKEN, {HOST}, {f"http://{HOST}"},
                     datasets={"synth": ds}, **kw)
    c = TestClient(app, base_url=f"http://{HOST}")
    c.headers["Authorization"] = f"Bearer {TOKEN}"
    return c


@pytest.fixture
def client(tmp_path, synth_store):
    with make_client(tmp_path, synth_store, results_dir=tmp_path / "results") as c:
        yield c


@pytest.fixture(autouse=True)
def _free_port(monkeypatch):
    """serve.main checks that its port is free; tests never listen (a running server must not fail them)."""
    monkeypatch.setattr("compass_studio.serve.port_busy", lambda host, port: None)
