"""Go-to-definition into sibling files and pinned libraries (#185): the bridge's
library URIs, and the read-only file routes the editor opens a definition through."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from scadbuddy.api.models import MAX_SOURCE_CHARS
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.settings import Settings
from scadbuddy.library import editor_files
from scadbuddy.library.lsp import LIBRARY_CLIENT_ROOT, _Roots, library_roots

# Answers every request with the params it saw (as a JSON string, so the bridge leaves
# it alone), its OPENSCADPATH, and a definition in the first library on that path.
FAKE_LSP = """#!/usr/bin/env python3
import json
import os
import pathlib
import sys

stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
search = [p for p in os.environ.get("OPENSCADPATH", "").split(os.pathsep) if p]


def send(message):
    body = json.dumps(message).encode()
    stdout.write(b"Content-Length: %d\\r\\n\\r\\n" % len(body) + body)
    stdout.flush()


while True:
    length = None
    while True:
        line = stdin.readline()
        if not line:
            raise SystemExit(0)
        if line == b"\\r\\n":
            break
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            length = int(value)
    message = json.loads(stdin.read(length))
    if "id" not in message:
        continue
    library = None
    if search:
        library = (pathlib.Path(search[0]) / "BOSL2" / "shapes3d.scad").as_uri()
    send({
        "jsonrpc": "2.0",
        "id": message["id"],
        "result": {
            "seen": json.dumps(message.get("params")),
            "path": json.dumps(os.environ.get("OPENSCADPATH")),
            "library": library,
            "sibling": (pathlib.Path.cwd() / "helper.scad").as_uri(),
        },
    })
"""

COMMIT = "0123456789abcdef0123456789abcdef01234567"
PIN = {
    "name": "BOSL2",
    "url": "https://example.invalid/BOSL2.git",
    "ref": "v1",
    "commit": COMMIT,
}


@pytest.fixture
def settings(settings: Settings, tmp_path: Path) -> Settings:
    binary = tmp_path / "fake-openscad-lsp"
    binary.write_text(FAKE_LSP, encoding="utf-8")
    binary.chmod(0o755)
    return settings.model_copy(update={"openscad_lsp": str(binary)})


def _pin(paths: DataPaths, model: str, pins: list[Any]) -> None:
    meta = json.loads(paths.model_meta(model).read_text(encoding="utf-8"))
    paths.model_meta(model).write_text(json.dumps({**meta, "libraries": pins}), encoding="utf-8")


@pytest.fixture
def library(paths: DataPaths, model: str) -> Path:
    """BOSL2 pinned to ``model``, its checkout on the volume; returns the directory
    `use <BOSL2/...>` resolves into."""
    _pin(paths, model, [PIN])
    directory = paths.libraries / "BOSL2" / COMMIT / "BOSL2"
    directory.mkdir(parents=True)
    (directory / "std.scad").write_text("include <shapes3d.scad>\n", encoding="utf-8")
    (directory / "shapes3d.scad").write_text("module cuboid(size) cube(size);\n", "utf-8")
    return directory


def _initialize(session: Any, model: str) -> dict[str, Any]:
    root = f"file:///models/{model}/"
    session.send_json(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"processId": None, "rootUri": root, "capabilities": {}},
        }
    )
    response: dict[str, Any] = session.receive_json()
    return response


# ── the bridge ────────────────────────────────────────────────────────────────


def test_each_library_gets_a_client_root_and_comes_first() -> None:
    roots = _Roots(
        (
            *library_roots({"BOSL2": Path("/data/libraries/BOSL2/c/BOSL2")}),
            ("file:///", "file:///data/models/demo/"),
        )
    )

    assert roots.outbound("file:///data/libraries/BOSL2/c/BOSL2/std.scad") == (
        LIBRARY_CLIENT_ROOT + "BOSL2@c/std.scad"
    )
    assert roots.inbound(LIBRARY_CLIENT_ROOT + "BOSL2@c/std.scad") == (
        "file:///data/libraries/BOSL2/c/BOSL2/std.scad"
    )
    # A directory that merely shares the prefix is not the library.
    assert roots.outbound("file:///data/libraries/BOSL2/c/BOSL2x/a.scad") == (
        "file:///data/libraries/BOSL2/c/BOSL2x/a.scad"
    )
    assert roots.outbound("file:///data/models/demo/helper.scad") == "file:///helper.scad"


def test_a_pinned_library_is_on_the_servers_path_under_a_client_uri_of_its_own(
    client: TestClient, model: str, library: Path
) -> None:
    with client.websocket_connect(f"/api/v1/models/{model}/lsp") as session:
        result = _initialize(session, model)["result"]
        session.send_json(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "textDocument/definition",
                "params": {
                    "textDocument": {"uri": f"{LIBRARY_CLIENT_ROOT}BOSL2@{COMMIT}/std.scad"}
                },
            }
        )
        again = session.receive_json()["result"]

    # The checkout's parent, as on a render, so `use <BOSL2/std.scad>` resolves.
    assert json.loads(result["path"]) == str(library.parent)
    # Scoped by the pinned commit, so a file the editor holds is never another pin's.
    assert result["library"] == f"{LIBRARY_CLIENT_ROOT}BOSL2@{COMMIT}/shapes3d.scad"
    assert result["sibling"] == f"file:///models/{model}/helper.scad"
    seen = json.loads(again["seen"])
    assert seen["textDocument"]["uri"] == (library / "std.scad").as_uri()


def test_a_model_without_pins_has_no_library_path(client: TestClient, model: str) -> None:
    with client.websocket_connect(f"/api/v1/models/{model}/lsp") as session:
        result = _initialize(session, model)["result"]

    assert json.loads(result["path"]) is None
    assert result["library"] is None


def test_an_unreadable_pin_still_gets_a_language_server(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """OpenSCAD's own check reports the bad pin; the editor keeps its completion."""
    _pin(paths, model, ["BOSL2"])
    with client.websocket_connect(f"/api/v1/models/{model}/lsp") as session:
        result = _initialize(session, model)["result"]

    assert json.loads(result["path"]) is None
    assert result["sibling"] == f"file:///models/{model}/helper.scad"


# ── a file in the model's directory ───────────────────────────────────────────


def test_a_sibling_file_is_served_as_text(client: TestClient, model: str, paths: DataPaths) -> None:
    (paths.model_dir(model) / "parts").mkdir()
    (paths.model_dir(model) / "parts" / "helper.scad").write_text("module h() {}\n", "utf-8")

    response = client.get(f"/api/v1/models/{model}/files/parts/helper.scad")

    assert response.status_code == 200, response.text
    assert response.text == "module h() {}\n"
    assert response.headers["content-type"] == "text/plain; charset=utf-8"


def test_the_model_source_itself_is_a_file_too(client: TestClient, model: str) -> None:
    response = client.get(f"/api/v1/models/{model}/files/model.scad")
    assert response.status_code == 200
    assert response.text.startswith("width = 10;")


@pytest.mark.parametrize(
    "path",
    [
        "%2E%2E/%2E%2E/secret.scad",
        "parts/%2E%2E/%2E%2E/secret.scad",
        "%2E/model.scad",
        ".git/config",
        ".renders/key/model.3mf",
        "parts/.hidden.scad",
        "%2Fetc%2Fpasswd",
        "parts//helper.scad",
        "parts%5Chelper.scad",
    ],
)
def test_a_path_that_is_not_plain_is_refused(
    client: TestClient, model: str, paths: DataPaths, path: str
) -> None:
    (paths.root / "secret.scad").write_text("TOP SECRET\n", encoding="utf-8")
    response = client.get(f"/api/v1/models/{model}/files/{path}")
    assert response.status_code == 422, (path, response.text)
    assert "TOP SECRET" not in response.text


def test_a_symlink_out_of_the_directory_reads_as_missing(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    outside = paths.root / "secret.scad"
    outside.write_text("TOP SECRET\n", encoding="utf-8")
    (paths.model_dir(model) / "link.scad").symlink_to(outside)
    (paths.model_dir(model) / "linked").symlink_to(paths.root)

    for path in ("link.scad", "linked/secret.scad"):
        response = client.get(f"/api/v1/models/{model}/files/{path}")
        assert response.status_code == 404, (path, response.text)
        assert "TOP SECRET" not in response.text


def test_a_symlink_inside_the_directory_is_followed(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    (paths.model_dir(model) / "alias.scad").symlink_to(paths.model_source(model))
    assert client.get(f"/api/v1/models/{model}/files/alias.scad").status_code == 200


def test_a_missing_file_a_directory_or_an_unknown_model_is_a_404(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    (paths.model_dir(model) / "parts").mkdir()
    assert client.get(f"/api/v1/models/{model}/files/nope.scad").status_code == 404
    assert client.get(f"/api/v1/models/{model}/files/parts").status_code == 404
    assert client.get("/api/v1/models/nobody/files/model.scad").status_code == 404


def test_a_binary_file_is_a_415(client: TestClient, model: str, paths: DataPaths) -> None:
    (paths.model_dir(model) / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\0\0\0\rIHDR")
    (paths.model_dir(model) / "latin1.scad").write_bytes("// caf\xe9\n".encode("latin-1"))

    assert client.get(f"/api/v1/models/{model}/files/logo.png").status_code == 415
    assert client.get(f"/api/v1/models/{model}/files/latin1.scad").status_code == 415


def test_a_file_over_the_source_cap_is_a_413(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    (paths.model_dir(model) / "big.scad").write_text("x" * (MAX_SOURCE_CHARS + 1), "utf-8")
    (paths.model_dir(model) / "at-cap.scad").write_text("x" * MAX_SOURCE_CHARS, "utf-8")

    assert client.get(f"/api/v1/models/{model}/files/big.scad").status_code == 413
    assert client.get(f"/api/v1/models/{model}/files/at-cap.scad").status_code == 200


# ── a file in a pinned library ────────────────────────────────────────────────


def test_a_library_file_is_served_from_the_models_pin(
    client: TestClient, model: str, library: Path
) -> None:
    response = client.get(f"/api/v1/models/{model}/libraries/BOSL2/files/shapes3d.scad")

    assert response.status_code == 200, response.text
    assert response.text == "module cuboid(size) cube(size);\n"


def test_a_library_file_for_a_commit_the_model_no_longer_pins_is_a_409(
    client: TestClient, model: str, library: Path
) -> None:
    """The editor's URI names the commit its session saw; after a re-pin the old
    file is not served under it, nor the new pin's file in its place."""
    route = f"/api/v1/models/{model}/libraries/BOSL2/files/shapes3d.scad"
    assert client.get(route, params={"commit": COMMIT}).status_code == 200

    moved = client.get(route, params={"commit": "f" * 40})
    assert moved.status_code == 409
    assert "0123456" in moved.json()["detail"]
    assert client.get(route, params={"commit": "HEAD"}).status_code == 422


def test_a_library_the_model_does_not_pin_is_a_404(
    client: TestClient, model: str, library: Path, paths: DataPaths
) -> None:
    other = paths.libraries / "MCAD" / COMMIT / "MCAD"
    other.mkdir(parents=True)
    (other / "units.scad").write_text("mm = 1;\n", encoding="utf-8")

    response = client.get(f"/api/v1/models/{model}/libraries/MCAD/files/units.scad")
    assert response.status_code == 404


def test_a_pinned_library_whose_checkout_is_gone_is_a_404(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    _pin(paths, model, [PIN])
    response = client.get(f"/api/v1/models/{model}/libraries/BOSL2/files/std.scad")
    assert response.status_code == 404


@pytest.mark.parametrize(
    "path", ["%2E%2E/%2E%2E/%2E%2E/secret.scad", ".git/config", "%2Fetc%2Fpasswd"]
)
def test_a_library_path_that_is_not_plain_is_refused(
    client: TestClient, model: str, library: Path, paths: DataPaths, path: str
) -> None:
    (paths.root / "secret.scad").write_text("TOP SECRET\n", encoding="utf-8")
    response = client.get(f"/api/v1/models/{model}/libraries/BOSL2/files/{path}")
    assert response.status_code == 422, (path, response.text)


def test_a_symlink_out_of_a_library_reads_as_missing(
    client: TestClient, model: str, library: Path, paths: DataPaths
) -> None:
    (paths.root / "secret.scad").write_text("TOP SECRET\n", encoding="utf-8")
    (library / "escape.scad").symlink_to(paths.root / "secret.scad")

    response = client.get(f"/api/v1/models/{model}/libraries/BOSL2/files/escape.scad")
    assert response.status_code == 404


def test_a_library_name_that_is_not_a_directory_name_is_a_422(
    client: TestClient, model: str
) -> None:
    response = client.get(f"/api/v1/models/{model}/libraries/.git/files/config")
    assert response.status_code == 422


def test_a_file_swapped_after_the_check_is_refused(
    client: TestClient, model: str, paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The descriptor that is read is checked again: one the kernel places outside the
    root (a path swapped between the resolve and the open) reads as missing."""
    outside = paths.root / "secret.scad"
    outside.write_text("TOP SECRET\n", encoding="utf-8")
    monkeypatch.setattr(editor_files, "opened_path", lambda fd: outside)

    response = client.get(f"/api/v1/models/{model}/files/model.scad")
    assert response.status_code == 404
    assert "TOP SECRET" not in response.text


def test_the_open_descriptor_is_where_the_file_is(tmp_path: Path) -> None:
    (tmp_path / "a.scad").write_text("x\n", encoding="utf-8")
    with (tmp_path / "a.scad").open("rb") as file:
        assert editor_files.opened_path(file.fileno()) == (tmp_path / "a.scad").resolve()
