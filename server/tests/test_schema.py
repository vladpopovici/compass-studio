"""The vendored schema matches COMPASS when a checkout is available."""

import ast
import os
from pathlib import Path

import pytest

from compass_studio.annot import schema

_REPO = Path(os.environ.get("COMPASS_REPO", Path(__file__).parents[2].parent / "COMPASS"))
_SRC = _REPO / "src/compass/core/annot/schema.py"


@pytest.mark.skipif(not _SRC.exists(), reason="no COMPASS checkout (set COMPASS_REPO)")
def test_matches_compass():
    vals = {}
    for n in ast.parse(_SRC.read_text()).body:
        t = getattr(n, "target", None) or (n.targets[0] if isinstance(n, ast.Assign) else None)
        if isinstance(t, ast.Name) and t.id in ("SQLITE_DDL", "SCHEMA_VERSION", "ANNOT_TYPE_CODE"):
            vals[t.id] = ast.literal_eval(n.value)
    assert vals["SQLITE_DDL"] == schema.SQLITE_DDL
    assert vals["SCHEMA_VERSION"] == schema.SCHEMA_VERSION
    assert vals["ANNOT_TYPE_CODE"] == schema.TYPE_CODE
