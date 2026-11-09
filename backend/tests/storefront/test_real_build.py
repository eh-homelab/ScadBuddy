"""A real ``pnpm build`` served through ``StorefrontStatic`` (#5282): every file the
``storefront.html`` entry loads, its CSS and fonts included, is answered 200.

Skipped unless ``SCADBUDDY_STOREFRONT_DIST`` names a built ``frontend/dist``; the
``frontend`` CI job builds one and runs this file (the backend lane has no build)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.routing import Mount

from scadbuddy.storefront.static import ENTRY, MANIFEST, StorefrontStatic

DIST = os.environ.get("SCADBUDDY_STOREFRONT_DIST")

pytestmark = pytest.mark.skipif(not DIST, reason="SCADBUDDY_STOREFRONT_DIST is not set")


def _files(manifest: dict[str, dict[str, object]]) -> set[str]:
    """Every ``file``, ``css`` and ``assets`` entry of the entry's import closure."""
    seen: set[str] = set()
    pending = [ENTRY]
    files: set[str] = set()
    while pending:
        key = pending.pop()
        if key in seen or key not in manifest:
            continue
        seen.add(key)
        chunk = manifest[key]
        files.add(str(chunk["file"]))
        files.update(chunk.get("css", []))  # type: ignore[arg-type]
        files.update(chunk.get("assets", []))  # type: ignore[arg-type]
        pending.extend(chunk.get("imports", []))  # type: ignore[arg-type]
        pending.extend(chunk.get("dynamicImports", []))  # type: ignore[arg-type]
    return files


def test_every_file_of_the_real_build_is_served() -> None:
    assert DIST is not None
    dist = Path(DIST)
    files = _files(json.loads((dist / MANIFEST).read_text("utf-8")))
    assert any(f.endswith(".css") for f in files)
    assert any(f.endswith(".woff2") for f in files), "storefront.css pulls in no font"
    client = TestClient(Starlette(routes=[Mount("/", app=StorefrontStatic(dist))]))
    refused = [f for f in sorted(files) if client.get(f"/{f}").status_code != 200]
    assert refused == []
