"""Contract v1 conformance: the server against openapi.yaml and schemas/.

Runs the Python server in-process on a small synthetic store (from
``server/``: ``uv run pytest ../contract/conformance``). Checks:

- the server's /api/v1 routes and the spec's operations are the same set;
- every JSON response validates against the spec's schema for its path,
  method and status code;
- every Arrow payload matches its schema file (fields, types, metadata);
- every operation in the spec is exercised at least once.
"""

import json
import re
from pathlib import Path

import pyarrow as pa
import pytest
import yaml
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from compass_studio.app import Dataset, create_app
from compass_studio.imaging.channels import SyntheticChannels
from compass_studio.imaging.labels import SyntheticLabels
from compass_studio.imaging.pyramid import SyntheticPyramid
from compass_studio.synth.generate import Params, generate

HERE = Path(__file__).parent
SPEC = yaml.safe_load((HERE.parent / "openapi.yaml").read_text())
SCHEMAS = {json.loads(p.read_text())["schema"]: json.loads(p.read_text())
           for p in (HERE.parent / "schemas").glob("*.v1.json")}
REGISTRY = Registry().with_resource("urn:spec", Resource.from_contents(SPEC, default_specification=DRAFT202012))
TOKEN, HOST = "conformance", "127.0.0.1:8765"
PREFIX = SPEC["servers"][0]["url"]
ARROW_TYPES = {"int64": pa.int64(), "uint16": pa.uint16(), "uint32": pa.uint32(), "float32": pa.float32(),
               "list<float32>": pa.list_(pa.float32()), "list<uint32>": pa.list_(pa.uint32())}


def _ptr(*parts: str) -> str:
    return "/".join(p.replace("~", "~0").replace("/", "~1") for p in parts)


def _template_regex(path: str) -> re.Pattern:
    return re.compile("^" + re.sub(r"\{[^}]+\}", r"[^/]+", re.escape(path).replace(r"\{", "{").replace(r"\}", "}"))
                      + "$")


TEMPLATES = [(p, _template_regex(p)) for p in SPEC["paths"]]


def spec_path(url_path: str) -> str:
    rel = url_path.split("?")[0][len(PREFIX):]
    hits = [p for p, rx in TEMPLATES if rx.match(rel)]
    assert hits, f"{rel} is not in the spec"
    return min(hits, key=lambda p: p.count("{"))  # most literal template wins


class Checked:
    """TestClient wrapper validating every /api/v1 response against the spec."""

    def __init__(self, client: TestClient):
        self.c, self.exercised = client, set()

    def request(self, method: str, url: str, **kw):
        r = self.c.request(method, PREFIX + url, **kw)
        path = spec_path(PREFIX + url)
        op = SPEC["paths"][path].get(method.lower())
        assert op is not None, f"{method} {path} is not in the spec"
        self.exercised.add((path, method.lower()))
        resp = op["responses"].get(str(r.status_code))
        assert resp is not None, f"{method} {url}: status {r.status_code} not documented"
        if r.status_code == 304:
            assert not r.content and "content" not in resp
            return r
        ref = resp.get("$ref")
        if ref:
            resp = SPEC["components"]["responses"][ref.rsplit("/", 1)[1]]
        ctype = r.headers.get("content-type", "").split(";")[0]
        content = resp.get("content", {})
        assert ctype in content, f"{method} {url}: content type {ctype!r} not documented"
        if ctype == "application/json":
            if ref:
                pointer = "#/" + _ptr("components", "responses", ref.rsplit("/", 1)[1], "content", ctype, "schema")
            else:
                pointer = "#/" + _ptr("paths", path, method.lower(), "responses", str(r.status_code),
                                      "content", ctype, "schema")
            v = Draft202012Validator({"$ref": "urn:spec" + pointer}, registry=REGISTRY)
            errors = sorted(v.iter_errors(r.json()), key=lambda e: e.path)
            assert not errors, f"{method} {url}: " + "; ".join(e.message for e in errors[:5])
        return r

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def post(self, url, **kw):
        return self.request("POST", url, **kw)


def check_arrow(r, expect: str | None = None) -> pa.Table:
    t = pa.ipc.open_stream(r.content).read_all()
    md = {k.decode(): v.decode() for k, v in (t.schema.metadata or {}).items()}
    name = md.get("schema")
    assert name in SCHEMAS, f"unknown binary schema {name!r}"
    if expect:
        assert name == expect
    spec = SCHEMAS[name]
    assert md.get("version") == spec["version"]
    assert [f.name for f in t.schema] == [f["name"] for f in spec["fields"]]
    for f, s in zip(t.schema, spec["fields"]):
        assert f.type == ARROW_TYPES[s["type"]], f"{name}.{f.name}: {f.type} != {s['type']}"
    assert set(spec["metadata"]) <= set(md), f"{name}: missing metadata {set(spec['metadata']) - set(md)}"
    return t


@pytest.fixture(scope="module")
def api(tmp_path_factory):
    d = tmp_path_factory.mktemp("conf")
    generate(d, "c", Params(width=3000, height=1500, points=300, cells=200, regions=2,
                            region_vertices=(40, 80), region_radius_px=(80.0, 200.0), attrs=4, lines=5),
             arrow=False, log=lambda m: None)
    ds = Dataset.open("conf", d / "c.ann.sqlite", d / "conf.curation.sqlite")
    labels = SyntheticLabels(2000, 1000)
    labels.aligned_to = "img"
    labels.classes = [{"value": 1, "name": "one", "color": "#112233"}, {"value": 2, "name": "two", "color": None}]
    app = create_app({"img": SyntheticPyramid(2000, 1000), "lab": labels, "mc": SyntheticChannels(2000, 1000, 3)}, TOKEN, {HOST}, {f"http://{HOST}"},
                     datasets={"conf": ds})
    with TestClient(app, base_url=f"http://{HOST}") as c:
        c.headers["Authorization"] = f"Bearer {TOKEN}"
        checked = Checked(c)
        checked.app = app
        yield checked


def test_routes_match_spec(api):
    served = set()
    for r in api.app.routes:
        path = getattr(r, "path", "")
        if path.startswith(PREFIX + "/"):
            for m in getattr(r, "methods", set()) - {"HEAD", "OPTIONS"}:
                served.add((path[len(PREFIX):].replace("{fmt}", "{format}"), m.lower()))
    specified = {(p, m) for p, ops in SPEC["paths"].items() for m in ops if m in ("get", "post", "put", "delete")}
    assert served == specified


def test_scenario(api):
    a = api
    info = a.get("/info").json()
    for name, spec in SCHEMAS.items():
        assert spec["version"] in info["schemas"][name]

    imgs = a.get("/images").json()
    a.get(f"/images/{imgs[0]['id']}")
    a.get("/images/nope")
    for fmt in ("jpg", "webp", "png"):
        r = a.get(f"/images/img/tiles/0/0/0.{fmt}")
        assert r.headers["content-type"] == {"jpg": "image/jpeg", "webp": "image/webp", "png": "image/png"}[fmt]
    a.get("/images/img/tiles/0/99/0.jpg")
    a.get("/images/img/tiles/0/0/0.bin")  # 400: not a label map
    lab = next(i for i in imgs if i.get("role") == "labels")
    assert lab["aligned_to"] == "img" and lab["dtype"] == "uint8"
    r = a.get(f"/images/{lab['id']}/tiles/0/0/0.bin", headers={"Accept-Encoding": "gzip"})
    assert r.headers["content-encoding"] == "gzip" and len(r.content) == 512 * 512
    a.get(f"/images/{lab['id']}/tiles/0/0/0.png")
    a.get(f"/images/{lab['id']}/tiles/0/0/0.jpg")  # 400
    ver = imgs[0]["version"]
    r = a.get(f"/images/img/tiles/0/0/0.jpg?v={ver}")
    assert "immutable" in r.headers["cache-control"] and r.headers["etag"] == f'"{ver}"'
    assert a.get("/images/img/tiles/0/0/0.jpg", headers={"If-None-Match": r.headers["etag"]}).status_code == 304
    a.get("/images/img/tiles/0/0/0.gif")
    # multichannel images (1.7.0)
    mc = next(i for i in imgs if i.get("display") == "channels")
    assert mc["channels"] == ["DAPI", "Marker 1", "Marker 2"] and mc["dtype"] == "uint16"
    r = a.get(f"/images/{mc['id']}/tiles/0/0/0.bin?channel=1", headers={"Accept-Encoding": "gzip"})
    assert r.headers["content-encoding"] == "gzip" and len(r.content) == 512 * 512 * 2
    a.get(f"/images/{mc['id']}/tiles/0/0/0.bin")  # 400: no channel
    a.get(f"/images/{mc['id']}/tiles/0/0/0.jpg?channel=0")  # 400
    a.get("/images/img/tiles/0/0/0.jpg?channel=0")  # 400: not multichannel
    h = a.get(f"/images/{mc['id']}/histogram").json()
    assert len(h["channels"]) == 3 and len(h["channels"][0]["counts"]) == 256
    a.get("/images/img/histogram")  # 400

    stores = a.get("/stores").json()
    sid = stores[0]["id"]
    a.get(f"/stores/{sid}")
    a.get("/stores/nope")
    a.get(f"/stores/{sid}/attributes")
    layers = a.get("/layers").json()
    a.get(f"/layers?store={sid}")
    points = next(lv for lv in layers if lv["counts"].get("point"))
    cells = next(lv for lv in layers if lv["counts"].get("polygon", 0) > 100)
    a.get(f"/layers/{cells['id']}")
    a.get("/layers/nope")

    check_arrow(a.get(f"/layers/{points['id']}/points"), "point-layer")
    lines = next(lv for lv in layers if lv["counts"].get("polyline"))
    lt = check_arrow(a.get(f"/layers/{lines['id']}/lines"), "line-layer")
    assert lt.num_rows == 5 and len(lt["coords"][0]) >= 40
    t = check_arrow(a.get(f"/layers/{cells['id']}/tiles/0/0/0"), "polygon-tile")
    check_arrow(a.get(f"/layers/{cells['id']}/tiles/{cells['polygon_max_level'] + 1}/0/0"), "centroid-tile")
    a.get(f"/layers/{cells['id']}/tiles/0/99/0")
    ids = ",".join(str(i) for i in t["object_id"].to_pylist()[:3])
    a.get(f"/layers/{cells['id']}/features?ids={ids}")
    a.get(f"/layers/{cells['id']}/objects?ids={ids}")
    a.get(f"/layers/{cells['id']}/objects?ids=abc")

    fork = a.post(f"/layers/{cells['id']}/fork", json={"name": "curated"}).json()
    a.post(f"/layers/{fork['id']}/fork")  # 400: sidecar layers cannot be forked
    a.post(f"/layers/{cells['id']}/fork", json={"name": ""})  # 422
    new = a.post(f"/stores/{sid}/layers", json={"name": "new", "groups": ["a", "b"]}).json()
    a.post(f"/stores/{sid}/layers", json={"name": "bad"})  # 422
    rev = a.get(f"/stores/{sid}/revision").json()["revision"]
    sq = {"type": "Polygon", "coordinates": [[[10, 10], [60, 10], [60, 50], [10, 50], [10, 10]]]}
    c = a.post(f"/stores/{sid}/commit", json={"base_revision": rev, "client": "conformance", "ops": [
        {"op": "create", "group_id": new["groups"][0]["id"], "geometry": sq, "values": {"q": 1.0}},
        {"op": "create", "group_id": new["groups"][1]["id"],
         "geometry": {"type": "Point", "coordinates": [5, 5]}}]}).json()
    oid = c["created"][0]
    a.post(f"/stores/{sid}/commit", json={"base_revision": c["revision"], "ops": [
        {"op": "move", "object_id": oid, "group_id": new["groups"][1]["id"]},
        {"op": "update_attributes", "object_id": oid, "values": {"q": None, "r": 2.5}},
        {"op": "update_geometry", "object_id": oid, "geometry": sq}]})
    a.post(f"/stores/{sid}/commit", json={"base_revision": rev, "ops": []})  # 409
    a.post(f"/stores/{sid}/commit", json={"base_revision": c["revision"] + 1, "ops": [{"op": "x"}]})  # 422
    a.post(f"/stores/{sid}/commit", content=b"not json")  # 400
    log = a.get(f"/stores/{sid}/log?since={rev}").json()["entries"]
    a.post(f"/stores/{sid}/commit", json={"base_revision": log[-1]["revision"], "ops": log[-1]["inverse"]})
    a.get(f"/stores/{sid}/log?limit=0")  # 400
    # save points (1.6.0)
    assert a.get(f"/layers/{new['id']}").json()["unsaved"] > 0
    assert a.post(f"/layers/{new['id']}/save").json()["unsaved"] == 0
    a.post(f"/stores/{sid}/commit", json={"base_revision": a.get(f"/stores/{sid}/revision").json()["revision"],
                                          "ops": [{"op": "delete", "object_id": oid}]})
    d = a.post(f"/layers/{new['id']}/discard", json={"client": "conformance"}).json()
    assert len(d["reverted"]) == 1 and a.get(f"/layers/{new['id']}").json()["unsaved"] == 0
    a.post(f"/layers/{cells['id']}/save")  # 400: store layers have no save point
    a.post(f"/layers/{cells['id']}/discard")  # 400
    a.get(f"/layers/{new['id']}/features?ids={oid}")
    check_arrow(a.get(f"/layers/{new['id']}/points"), "point-layer")
    a.get("/info", headers={"Authorization": "Bearer wrong"})  # 401

    with a.c.stream("GET", PREFIX + "/events") as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        lines = []
        for line in r.iter_lines():
            lines.append(line)
            if line.startswith("data: "):
                break
    assert lines[0] == "event: hello" and "revisions" in json.loads(lines[1][6:])
    a.exercised.add(("/events", "get"))

    specified = {(p, m) for p, ops in SPEC["paths"].items() for m in ops if m in ("get", "post")}
    assert specified - a.exercised == set()


def test_validator_rejects_bad_bodies():
    """Guard against a validator that silently accepts everything."""
    def errs(pointer, body):
        v = Draft202012Validator({"$ref": "urn:spec#/" + pointer}, registry=REGISTRY)
        return list(v.iter_errors(body))
    assert errs("components/schemas/Layer", {"id": "x"})  # missing fields
    assert errs("components/schemas/ObjectId", 2**53)  # above the JSON-safe limit
    assert errs("components/schemas/CommitRequest", {"base_revision": 0, "ops": [{"op": "teleport"}]})
    assert not errs("components/schemas/CommitRequest", {"base_revision": 0, "ops": [
        {"op": "delete", "object_id": 5}]})
