"""ZarrPyramid metadata, version fingerprint and image folders."""

import json
import os

import numpy as np
import pytest
import zarr

from compass_studio.imaging.pyramid import ZarrPyramid, find_images
from compass_studio.serve import main as serve_main


def _image(path, mpp=0.5, power=20.0):
    g = zarr.open_group(str(path), mode="w", zarr_format=3)
    g.attrs.update({"level_count": 2, "base_mpp": mpp, "base_objective_power": power,
                    "channel_names": ["R", "G", "B"], "import": {"source": {"path": "/secret/x.mrxs"}}})
    for i, n in enumerate((1024, 512)):
        a = g.create_array(str(i), shape=(n, n, 3), dtype="uint8", chunks=(512, 512, 3),
                           shards=(512, 512, 3))
        a[:] = np.full((n, n, 3), 40 * i, np.uint8)
    return path


def test_metadata_and_version(tmp_path):
    p = ZarrPyramid(_image(tmp_path / "S-01.zarr"))
    info = p.info()
    assert info["name"] == "S-01" and info["objective_power"] == 20.0 and info["mpp"] == 0.5
    assert len(info["version"]) == 16
    assert "secret" not in json.dumps(info)  # provenance never leaks
    assert ZarrPyramid(tmp_path / "S-01.zarr").version == p.version  # stable
    zj = tmp_path / "S-01.zarr" / "zarr.json"
    st = zj.stat()
    os.utime(zj, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))  # re-import
    assert ZarrPyramid(tmp_path / "S-01.zarr").version != p.version


def test_find_images(tmp_path):
    _image(tmp_path / "a b.zarr")
    _image(tmp_path / "c.zarr")
    (tmp_path / "d.zarr").mkdir()  # no zarr.json
    (tmp_path / "e.txt").write_text("x")
    assert {k: v.name for k, v in find_images(tmp_path).items()} == {"a-b": "a b.zarr", "c": "c.zarr"}
    _image(tmp_path / "a-b.zarr")
    with pytest.raises(ValueError):
        find_images(tmp_path)


def test_serve_rejects_empty_folder(tmp_path, capsys):
    with pytest.raises(SystemExit):
        serve_main(["--images", str(tmp_path)])
    assert "no .zarr images" in capsys.readouterr().err
