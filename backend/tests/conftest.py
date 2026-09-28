from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import uuid
import zipfile
import zlib
from collections.abc import Iterator
from ipaddress import ip_address
from pathlib import Path
from typing import Any

import numpy as np
import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from scadbuddy.core import settings as settings_module
from scadbuddy.core.config import load_config
from scadbuddy.library import url_import
from scadbuddy.library.history import GIT, git_env
from tests.support.temporal import (
    TEST_TEMPORAL_ADDRESS_ENV,
    TEST_TEMPORAL_DEV_SERVER_ENV,
    temporal_available,
)

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN = Path(__file__).parent / "golden"


def load_fixture_param(stem: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / f"{stem}.param").read_text(encoding="utf-8"))
    return data


def load_fixture_source(stem: str) -> str:
    return (FIXTURES / f"{stem}.scad").read_text(encoding="utf-8")


def openscad_binary() -> str | None:
    return shutil.which(load_config().openscad)


def openscad_lsp_binary() -> str | None:
    return shutil.which(load_config().openscad_lsp)


def git_binary() -> str | None:
    """`git` is baked into every image stage, so this skip is dead where CI runs
    the suite -- it is a developer convenience, not a supported configuration."""
    return shutil.which(GIT)


def make_library_upstream(root: Path, versions: dict[str, str]) -> tuple[str, dict[str, str]]:
    """A bare repository standing in for a library's upstream (#93): one commit and
    one tag per entry of ``versions`` (tag -> the ``std.scad`` at that tag).

    Returns its ``file://`` URL and each tag's commit. Local, so the library tests
    exercise the real ``git clone`` without ever touching the network.
    """
    work = root / "upstream-work"
    bare = root / "upstream.git"
    env = git_env()

    def git(*args: str, cwd: Path = work) -> str:
        return subprocess.run(
            [GIT, *args], cwd=cwd, env=env, capture_output=True, text=True, check=True
        ).stdout.strip()

    work.mkdir(parents=True)
    git("init", "--initial-branch=main", ".")
    commits: dict[str, str] = {}
    for tag, source in versions.items():
        (work / "std.scad").write_text(source, encoding="utf-8")
        git("add", "-A")
        git("commit", "-m", f"release {tag}")
        git("tag", tag)
        commits[tag] = git("rev-parse", "HEAD")
    git("clone", "--bare", str(work), str(bare), cwd=root)
    return f"file://{bare}", commits


def installed_font_families() -> str:
    """`fc-list` output, lowercased. Empty when fontconfig is absent — which reads as
    "the family is missing", the safe answer: a missing face is substituted silently."""
    if shutil.which("fc-list") is None:
        return ""
    return subprocess.run(["fc-list"], capture_output=True, text=True, check=False).stdout.lower()


@pytest.fixture(autouse=True)
def _skip_without_openscad(request: pytest.FixtureRequest) -> None:
    if request.node.get_closest_marker("requires_openscad") and openscad_binary() is None:
        pytest.skip("openscad is not on PATH")


@pytest.fixture(autouse=True)
def _skip_without_openscad_lsp(request: pytest.FixtureRequest) -> None:
    if request.node.get_closest_marker("requires_openscad_lsp") and openscad_lsp_binary() is None:
        pytest.skip("openscad-lsp is not on PATH")


#: A Postgres the tests may create and drop schemas in. Unset, `requires_postgres`
#: tests skip; CI sets it to a service container.
TEST_DATABASE_URL_ENV = "SCADBUDDY_TEST_DATABASE_URL"


# Read once, at import: the API tests scrub every SCADBUDDY_* variable from the
# environment so none leaks into their Settings.
_POSTGRES_URL = os.environ.get(TEST_DATABASE_URL_ENV) or None


def postgres_url() -> str | None:
    return _POSTGRES_URL


@pytest.fixture(autouse=True)
def _skip_without_postgres(request: pytest.FixtureRequest) -> None:
    if request.node.get_closest_marker("requires_postgres") and postgres_url() is None:
        pytest.skip(f"{TEST_DATABASE_URL_ENV} is not set")


@pytest.fixture(autouse=True)
def _skip_without_temporal(request: pytest.FixtureRequest) -> None:
    if request.node.get_closest_marker("requires_temporal") and not temporal_available():
        pytest.skip(
            f"no Temporal: set {TEST_TEMPORAL_ADDRESS_ENV} (a running server) or"
            f" {TEST_TEMPORAL_DEV_SERVER_ENV} (a temporal CLI), or put `temporal` on PATH"
        )


#: For a `Settings` whose app never starts: the database URL is required (#401), but
#: nothing is dialled until the lifespan opens the stores.
UNUSED_DATABASE_URL = "postgresql://unused.invalid/scadbuddy"


@pytest.fixture
def pg_conninfo() -> Iterator[str]:
    """A throwaway schema on the test Postgres, dropped afterwards.

    Skips the test without one: every test that starts the app needs it, since the
    settings live in Postgres (#401).
    """
    url = postgres_url()
    if url is None:
        pytest.skip(f"{TEST_DATABASE_URL_ENV} is not set")
    schema = f"test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"'.encode())
    try:
        yield make_conninfo(url, options=f"-c search_path={schema}")
    finally:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE'.encode())


@pytest.fixture(autouse=True)
def _no_image_library_seed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Inside the test image, /app/libraries holds the baked-in BOSL2; an app a test
    starts must not copy it into every temporary data directory. A test of the
    seed passes ``seed_libraries_dir`` explicitly."""
    monkeypatch.setattr(settings_module, "CONTAINER_SEED_LIBRARIES_DIR", tmp_path / "no-seed")


@pytest.fixture(autouse=True)
def _skip_without_git(request: pytest.FixtureRequest) -> None:
    if request.node.get_closest_marker("requires_git") and git_binary() is None:
        pytest.skip("git is not on PATH")


def write_openscad_3mf(
    path: Path,
    parts: list[tuple[str, str, Any]],
    *,
    default_colour: str = "#F9D72C00",
) -> Path:
    """Write a 3MF shaped like OpenSCAD's Manifold export: one object, basematerials,
    per-triangle material index, index 0 reserved for an unused Default."""
    bases = [f'<base name="Default" displaycolor="{default_colour}" />']
    vertices: list[str] = []
    triangles: list[str] = []
    offset = 0
    for index, (name, colour, mesh) in enumerate(parts, start=1):
        bases.append(f'<base name="{name}" displaycolor="{colour}" />')
        for vertex in mesh.vertices:
            vertices.append(f'<vertex x="{vertex[0]:f}" y="{vertex[1]:f}" z="{vertex[2]:f}" />')
        for face in mesh.faces:
            triangles.append(
                f'<triangle v1="{face[0] + offset}" v2="{face[1] + offset}" '
                f'v3="{face[2] + offset}" pid="1" p1="{index}" />'
            )
        offset += len(mesh.vertices)
    model = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
        'unit="millimeter" xml:lang="en-US">\n'
        "<resources>\n"
        '<basematerials id="1">' + "".join(bases) + "</basematerials>\n"
        '<object id="2" name="OpenSCAD Model" type="model" pid="1" pindex="0"><mesh>'
        "<vertices>" + "".join(vertices) + "</vertices>"
        "<triangles>" + "".join(triangles) + "</triangles>"
        "</mesh></object>\n"
        "</resources>\n"
        '<build><item objectid="2" /></build>\n'
        "</model>\n"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("3D/3dmodel.model", model)
    return path


def read_png(data: bytes) -> np.ndarray:
    """Decode an 8-bit RGBA PNG to an HxWx4 array.

    Only what `scadbuddy.render.thumbnail.encode_png` emits: colour type 6, bit
    depth 8, no interlacing, filter type 0 on every row. Anything else raises, so
    a reader that silently coped with a malformed image cannot make a test pass.
    """
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    chunks: dict[bytes, bytes] = {}
    idat = b""
    offset = 8
    while offset < len(data):
        (length,) = struct.unpack(">I", data[offset : offset + 4])
        kind = data[offset + 4 : offset + 8]
        payload = data[offset + 8 : offset + 8 + length]
        (checksum,) = struct.unpack(">I", data[offset + 8 + length : offset + 12 + length])
        if zlib.crc32(kind + payload) != checksum:
            raise ValueError(f"bad CRC on {kind!r}")
        if kind == b"IDAT":
            idat += payload
        else:
            chunks[kind] = payload
        offset += 12 + length
    width, height, depth, colour, compression, filtering, interlace = struct.unpack(
        ">IIBBBBB", chunks[b"IHDR"]
    )
    if (depth, colour, compression, filtering, interlace) != (8, 6, 0, 0, 0):
        raise ValueError("expected an uninterlaced 8-bit RGBA PNG")
    stride = width * 4
    raw = zlib.decompress(idat)
    if len(raw) != height * (stride + 1):
        raise ValueError("truncated image data")
    rows = np.frombuffer(raw, dtype=np.uint8).reshape(height, stride + 1)
    if rows[:, 0].any():
        raise ValueError("expected filter type 0 on every row")
    return rows[:, 1:].reshape(height, width, 4)


#: Any globally routable address; nothing ever connects to it.
PUBLIC_ADDRESS = "93.184.215.14"


@pytest.fixture
def fake_dns(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """Name resolution for the URL import, without the network.

    An address literal resolves to itself, as `getaddrinfo` does; any other name to
    `PUBLIC_ADDRESS` unless the test maps it to something else in the returned dict.
    """
    answers: dict[str, list[str]] = {}

    async def resolve(host: str, port: int) -> list[str]:
        try:
            return [str(ip_address(host))]
        except ValueError:
            return answers.get(host, [PUBLIC_ADDRESS])

    monkeypatch.setattr(url_import, "resolve_host", resolve)
    return answers
