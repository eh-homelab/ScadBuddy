from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import time
import uuid
import zipfile
import zlib
from collections.abc import Callable, Iterator, Mapping
from datetime import timedelta
from ipaddress import ip_address
from pathlib import Path
from typing import Any

import numpy as np
import psycopg
import pytest
import trimesh
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from psycopg import Connection, sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool

from scadbuddy.core import settings as settings_module
from scadbuddy.core.config import Config, load_config
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.settings import Settings
from scadbuddy.core.trace_scrub import ScrubbingSpanExporter
from scadbuddy.core.tracing import DEFAULT_SAMPLER
from scadbuddy.library import url_import
from scadbuddy.library.assets import AssetStore
from scadbuddy.library.history import GIT, git_env
from scadbuddy.render import confinement
from scadbuddy.render.job_models import Job, JobResult, now
from scadbuddy.render.jobs import render_job
from scadbuddy.render.pg_store import migrate
from scadbuddy.render.schema import ParamValue
from scadbuddy.workflows.commands import start_command
from tests.support.rack_guard import foreign_rack_errors
from tests.support.temporal import (
    TEST_TEMPORAL_ADDRESS_ENV,
    TEST_TEMPORAL_DEV_SERVER_ENV,
    temporal_available,
)

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN = Path(__file__).parent / "golden"

#: Every span any test makes, after the same scrub production uses (spec §6).
_SPANS = InMemorySpanExporter()
_provider = TracerProvider(sampler=DEFAULT_SAMPLER)
_provider.add_span_processor(SimpleSpanProcessor(ScrubbingSpanExporter(_SPANS)))
trace.set_tracer_provider(_provider)


@pytest.fixture(autouse=True)
def _clear_spans() -> Iterator[None]:
    """Every test's spans go after it, whether it read them or not: the exporter lives
    for the whole session and would otherwise hold every span every test made."""
    yield
    _SPANS.clear()


@pytest.fixture
def spans() -> InMemorySpanExporter:
    return _SPANS


def wait_for_span(
    spans: InMemorySpanExporter, predicate: Callable[[ReadableSpan], bool], timeout: float = 30
) -> ReadableSpan:
    """Spans end on the worker's own tasks after the job settles: poll, bounded."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for finished in spans.get_finished_spans():
            if predicate(finished):
                return finished
        time.sleep(0.05)
    raise AssertionError("no matching span was recorded")


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
def _sandbox_only_real_openscad(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fake openscads are scripts reading and writing the test's own files, which
    the sandbox (#994) rightly refuses them; a test of the real binary keeps it."""
    if not request.node.get_closest_marker("requires_openscad"):
        monkeypatch.setattr(confinement, "landlock_abi", lambda: 0)


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


#: The answer deadline `start_command` takes when its caller names none: a route's
#: inline window and its accept bound. Production's 10 s is a promise about latency
#: that a loaded machine breaks (a 202, or a 503 `command-still-accepting`, where the
#: test asserts the final answer), so tests wait this long instead. A test of the
#: deadline itself names one, or sets this default back. The print route names its own,
#: which `tests/api/conftest.py` sets (review #1316 4a).
TEST_ANSWER_DEADLINE = timedelta(seconds=60)


@pytest.fixture(autouse=True)
def _answer_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    defaults = start_command.__kwdefaults__
    assert defaults is not None
    monkeypatch.setitem(defaults, "deadline", TEST_ANSWER_DEADLINE)


#: For a `Settings` whose app never starts: the database URL is required (#401), but
#: nothing is dialled until the lifespan opens the stores.
UNUSED_DATABASE_URL = "postgresql://unused.invalid/scadbuddy"
#: The same for the Temporal address (#546). Nothing listens on port 1, and the API's
#: client is lazy, so an app built with it boots and its renders wait, unstarted.
UNUSED_TEMPORAL_ADDRESS = "127.0.0.1:1"


@pytest.fixture(scope="session")
def _pg_database_url() -> Iterator[str | None]:
    """The database this process's tests make their schemas in: the test database
    itself, or under pytest-xdist one of its own per worker, dropped afterwards.

    A schema per test is not isolation enough once tests run at once: advisory locks
    (`migrate`'s fixed MIGRATION_LOCK among them) and LISTEN/NOTIFY channels (the
    event bus's `scadbuddy_events`) are per DATABASE, so two workers sharing one
    would serialise every migration and hear each other's events."""
    url = postgres_url()
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    if url is None or worker is None:
        yield url
        return
    database = f"{conninfo_to_dict(url).get('dbname') or 'postgres'}_{worker}"
    create = sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database))
    drop = sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database))
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(drop)  # a killed run's
        conn.execute(create)
    try:
        yield make_conninfo(url, dbname=database)
    finally:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(drop)


@pytest.fixture
def pg_conninfo(_pg_database_url: str | None) -> Iterator[str]:
    """A throwaway schema on the test Postgres, dropped afterwards.

    Skips the test without one: every test that starts the app needs it, since the
    settings live in Postgres (#401).
    """
    url = _pg_database_url
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


PgPool = ConnectionPool[Connection[DictRow]]


def open_pg_pool(conninfo: str, *, size: int = 4) -> PgPool:
    """A migrated pool on ``conninfo``, shaped as the render queue's (autocommit, dict
    rows): what the stores that keep no pool of their own are given."""
    pool: PgPool = ConnectionPool(
        conninfo,
        min_size=1,
        max_size=size,
        open=False,
        connection_class=Connection[DictRow],
        kwargs={"autocommit": True, "row_factory": dict_row},
    )
    pool.open(wait=True, timeout=30)
    with pool.connection() as conn:
        migrate(conn)
    return pool


@pytest.fixture
def pg_pool(pg_conninfo: str) -> Iterator[PgPool]:
    """`open_pg_pool` on the test's throwaway schema, closed afterwards."""
    pool = open_pg_pool(pg_conninfo)
    try:
        yield pool
    finally:
        pool.close()


MODEL_SLUG = "demo"

# A stand-in for the real binary: enough to answer --version and to export a .param,
# so the routes that shell out are exercised where no openscad is installed.
FAKE_OPENSCAD = """#!/usr/bin/env python3
import json
import os
import pathlib
import shutil
import sys

args = sys.argv[1:]
# Test settings sit beside the binary: the backend passes openscad no FAKE_* variable.
sidecar = pathlib.Path(sys.argv[0]).with_name("fake-env.json")
settings = json.loads(sidecar.read_text(encoding="utf-8")) if sidecar.is_file() else {}

# Lets a test count how many times openscad was actually run.
log = settings.get("FAKE_OPENSCAD_LOG")
if log:
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(" ".join(args) + "\\n")
# And what OPENSCADPATH it was given (#93).
path_log = settings.get("FAKE_OPENSCAD_PATH_LOG")
if path_log:
    with open(path_log, "a", encoding="utf-8") as handle:
        handle.write(os.environ.get("OPENSCADPATH", "") + "\\n")
if "--version" in args:
    print("OpenSCAD version 2099.01.01", file=sys.stderr)  # the real one uses stderr too
    raise SystemExit(0)

out = None
for index, arg in enumerate(args):
    if arg == "-o" and index + 1 < len(args):
        out = args[index + 1]

source = pathlib.Path(args[-1])
text = source.read_text(encoding="utf-8", errors="replace") if source.is_file() else ""
if "%%FAIL%%" in text:
    print("ERROR: Parser error: syntax error", file=sys.stderr)
    raise SystemExit(1)

if "%%BADPARAM%%" in text and out is not None and out.endswith(".param"):
    # Exit 0, and an export with a parameter that has no name.
    pathlib.Path(out).write_text(json.dumps({"parameters": [{"type": "number"}]}))
    raise SystemExit(0)

if "%%RANGED%%" in text and out is not None and out.endswith(".param"):
    # A customizer range and a select, as `// [1:100]` and `// [a, b]` export (#432).
    pathlib.Path(out).write_text(
        json.dumps(
            {
                "parameters": [
                    {"name": "width", "type": "number", "initial": 10, "group": "Main",
                     "min": 1, "max": 100, "step": 1},
                    {"name": "shape", "type": "string", "initial": "round", "group": "Main",
                     "options": [{"name": "Round", "value": "round"},
                                 {"name": "Square", "value": "square"}]},
                ],
            }
        )
    )
    raise SystemExit(0)

if out is not None and out.endswith(".3mf"):
    # The API tests' render (tests/api/conftest.py): `width=999` fails as a template
    # that could not open its picture does; anything else prints FAKE_STDERR and
    # exports the 3MF named FAKE_3MF, if any.
    if any(arg.startswith("width=999") for arg in args):
        print("WARNING: The file 'pic.svg' couldn't be opened", file=sys.stderr)
        print("ERROR: something broke", file=sys.stderr)
        raise SystemExit(1)
    for line in settings.get("FAKE_STDERR", []):
        print(line, file=sys.stderr)
    if "FAKE_3MF" in settings:
        shutil.copyfile(settings["FAKE_3MF"], out)
    raise SystemExit(0)

if out is not None and out.endswith(".param"):
    pathlib.Path(out).write_text(
        json.dumps(
            {
                "title": "Fake",
                "parameters": [
                    {"name": "width", "type": "number", "initial": 10, "group": "Main"},
                    {"name": "label", "type": "string", "initial": "hi", "group": "Main"},
                ],
            }
        )
    )
raise SystemExit(0)
"""


@pytest.fixture
def fake_openscad(tmp_path: Path) -> str:
    binary = tmp_path / "fake-openscad"
    binary.write_text(FAKE_OPENSCAD, encoding="utf-8")
    binary.chmod(0o755)
    return str(binary)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def seed_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "seed"
    directory.mkdir()
    return directory


@pytest.fixture
def settings(data_dir: Path, seed_dir: Path, fake_openscad: str, pg_conninfo: str) -> Settings:
    """The app's settings, on a throwaway Postgres schema: it will not start without one."""
    return Settings(
        openscad=fake_openscad,
        data_dir=data_dir,
        seed_models_dir=seed_dir,
        frontend_dir=Path("/nonexistent"),
        database_url=pg_conninfo,
        temporal_address=UNUSED_TEMPORAL_ADDRESS,
        # Off, so no test renders a preview behind its back; `test_previews`
        # turns them on with a stub render.
        preview_renders=False,
    )


@pytest.fixture
def paths(data_dir: Path) -> DataPaths:
    data = DataPaths(data_dir)
    data.ensure()
    return data


@pytest.fixture
def model(paths: DataPaths) -> str:
    paths.model_dir(MODEL_SLUG).mkdir(parents=True, exist_ok=True)
    paths.model_source(MODEL_SLUG).write_text('width = 10;\nlabel = "hi";\n', encoding="utf-8")
    paths.model_meta(MODEL_SLUG).write_text(
        json.dumps({"name": "Demo", "description": "a demo", "tags": ["test"]}) + "\n",
        encoding="utf-8",
    )
    return MODEL_SLUG


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


#: Writes a `.param`, copies the 3MF named in `fake-env.json` to every `.3mf` output,
#: and exits 1 on a source containing `%%FAIL%%`.
FAKE_3MF_OPENSCAD = """#!/usr/bin/env python3
import json
import pathlib
import shutil
import sys

args = sys.argv[1:]
settings = json.loads(pathlib.Path(sys.argv[0]).with_name("fake-env.json").read_text())
out = args[args.index("-o") + 1] if "-o" in args else None
source = pathlib.Path(args[-1])
if "%%FAIL%%" in source.read_text(encoding="utf-8"):
    print("ERROR: Parser error: syntax error", file=sys.stderr)
    raise SystemExit(1)
if out is not None and out.endswith(".param"):
    pathlib.Path(out).write_text(
        json.dumps({"parameters": [{"name": "width", "type": "number", "initial": 10}]})
    )
elif out is not None and out.endswith(".3mf"):
    shutil.copyfile(settings["FAKE_3MF"], out)
"""


def fake_3mf_openscad(directory: Path) -> str:
    """A fake openscad in ``directory`` whose every 3MF export is one blue box."""
    binary = directory / "fake-openscad"
    directory.mkdir(parents=True)
    binary.write_text(FAKE_3MF_OPENSCAD, encoding="utf-8")
    binary.chmod(0o755)
    model = write_openscad_3mf(
        directory / "drawn.3mf",
        [("Color 1", "#0047BB00", trimesh.creation.box(extents=(10, 10, 2)))],
    )
    (directory / "fake-env.json").write_text(json.dumps({"FAKE_3MF": str(model)}))
    return str(binary)


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


async def render_once(
    paths: DataPaths, slug: str, params: Mapping[str, ParamValue]
) -> tuple[Job, JobResult]:
    """Render ``slug`` through the production pipeline (`render_job`: the stages the
    worker's activities run, in one process) with the configured `openscad`."""
    job = Job(id=uuid.uuid4().hex, slug=slug, params=dict(params), created_at=now())
    config = Config(openscad=load_config().openscad, data_dir=paths.root)
    result, log_tail = await render_job(
        job, config=config, paths=paths, assets=AssetStore(paths.assets)
    )
    job.state, job.result, job.log_tail = "done", result, log_tail
    return job, result


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


@pytest.fixture(autouse=True)
def rack_pick_swallows_only_expected_errors(
    request: pytest.FixtureRequest, caplog: pytest.LogCaptureFixture
) -> Iterator[None]:
    """A rack fallback (the pick, the /check preview, the usage read, or the store's
    seen, picks and settle writes and reads, #1112) swallows every exception by spec, so
    this is where a programming error (TypeError, KeyError...) surfaces. A test that
    needs another type on purpose opts out with ``@pytest.mark.rack_injects_errors``,
    which turns the guard off for every fallback in that test: when an expected type
    (``ApiError``, ``psycopg.OperationalError``) proves the same point, inject that."""
    yield
    if request.node.get_closest_marker("rack_injects_errors"):
        return
    foreign = foreign_rack_errors(caplog.get_records("call"))
    assert not foreign, f"a rack fallback swallowed a programming error: {foreign}"
