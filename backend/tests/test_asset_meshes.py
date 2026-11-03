"""STL, 3MF and OBJ uploads for `// file` parameters (#959): read by bounded readers,
stored in a canonical form, refused with a reason."""

from __future__ import annotations

import io
import struct
import zipfile

import numpy as np
import pytest
import trimesh

from scadbuddy.library import meshes
from scadbuddy.library.assets import AssetRejectedError, AssetStore, sniff
from scadbuddy.library.meshes import MeshRejectedError, normalise_mesh, preview_png
from scadbuddy.meshmodel.ingest import part_mesh_3mf
from tests.conftest import PgPool

BOX = trimesh.creation.box(extents=(10.0, 20.0, 30.0))


def binary_stl(mesh: trimesh.Trimesh, header: bytes = b"made by someone, with a note") -> bytes:
    triangles = mesh.vertices[mesh.faces].astype("<f4")
    body = b"".join(b"\0" * 12 + t.tobytes() + b"\0\0" for t in triangles)
    return header.ljust(80, b" ") + struct.pack("<I", len(triangles)) + body


def ascii_stl(mesh: trimesh.Trimesh) -> bytes:
    lines = ["solid box"]
    for a, b, c in mesh.vertices[mesh.faces]:
        lines += ["facet normal 0 0 0", "outer loop"]
        lines += [f"vertex {x} {y} {z}" for x, y, z in (a, b, c)]
        lines += ["endloop", "endfacet"]
    return ("\n".join([*lines, "endsolid box"]) + "\n").encode()


def obj_text(mesh: trimesh.Trimesh) -> bytes:
    lines = ["# a box", "mtllib box.mtl", "o box", "usemtl red", "vt 0 0", "vn 0 0 1"]
    lines += [f"v {x} {y} {z}" for x, y, z in mesh.vertices]
    lines += [f"f {a + 1}/1/1 {b + 1}//1 {c + 1}" for a, b, c in mesh.faces]
    return ("\n".join(lines) + "\n").encode()


def test_each_kind_is_sniffed_from_its_bytes() -> None:
    assert sniff(binary_stl(BOX, b"solid but binary")) == "stl"
    assert sniff(ascii_stl(BOX)) == "stl"
    assert sniff(part_mesh_3mf(BOX)) == "3mf"
    assert sniff(obj_text(BOX)) == "obj"


@pytest.mark.parametrize("make", [binary_stl, ascii_stl])
def test_an_stl_is_stored_as_binary_with_nothing_of_the_senders(make: object) -> None:
    stored = normalise_mesh("stl", make(BOX))  # type: ignore[operator]
    assert stored.data[:80] == bytes(80)
    assert stored.triangles == 12
    assert stored.bounds == [-5.0, -10.0, -15.0, 5.0, 10.0, 15.0]
    again = trimesh.load(io.BytesIO(stored.data), file_type="stl")
    assert np.allclose(again.bounds, BOX.bounds)


def test_an_obj_keeps_its_vertices_and_faces_only() -> None:
    quad = b"v 0 0 0\nv 1 0 0\nv 1 1 0\nv 0 1 0\nmtllib evil.mtl\nf -4 -3 -2 -1\n"
    stored = normalise_mesh("obj", quad)
    assert stored.triangles == 2
    assert stored.data.decode().splitlines() == [
        "v 0 0 0",
        "v 1 0 0",
        "v 1 1 0",
        "v 0 1 0",
        "f 1 2 3",
        "f 1 3 4",
    ]
    assert normalise_mesh("obj", obj_text(BOX)).triangles == 12


def test_a_3mf_of_one_object_is_rewritten_as_its_triangles() -> None:
    stored = normalise_mesh("3mf", part_mesh_3mf(BOX))
    assert stored.triangles == 12
    with zipfile.ZipFile(io.BytesIO(stored.data)) as archive:
        assert sorted(archive.namelist()) == [
            "3D/3dmodel.model",
            "[Content_Types].xml",
            "_rels/.rels",
        ]
        assert all(i.compress_type == zipfile.ZIP_DEFLATED for i in archive.infolist())


@pytest.mark.parametrize(
    ("kind", "data", "reason"),
    [
        ("obj", b"v 0 0 0\nv 1 0 0\nf 1 2 3\n", "vertex"),
        ("obj", b"v 0 0 nan\nv 1 0 0\nv 0 1 0\nf 1 2 3\n", "finite"),
        ("obj", b"v 0 0 1e9\nv 1 0 0\nv 0 1 0\nf 1 2 3\n", "mm"),
        ("obj", b"v 0 0 0\n", "no triangles"),
        ("obj", b"\xff\xfe v 0 0 0", "UTF-8"),
        ("stl", b"solid x\nfacet normal 0 0 0\nendsolid\n", "STL"),
        ("3mf", b"PK\x03\x04 not a zip", "3MF"),
    ],
)
def test_what_cannot_be_read_faithfully_is_refused(kind: str, data: bytes, reason: str) -> None:
    with pytest.raises(MeshRejectedError, match=reason):
        normalise_mesh(kind, data)  # type: ignore[arg-type]


def test_a_3mf_of_two_objects_is_refused() -> None:
    other = trimesh.creation.box(extents=(1.0, 1.0, 1.0))
    scene = trimesh.Scene([BOX, other])
    data = scene.export(file_type="3mf")
    with pytest.raises(MeshRejectedError, match="one object"):
        normalise_mesh("3mf", data)


def test_a_zip_bomb_is_refused_before_it_is_inflated() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("3D/3dmodel.model", b"<" + b" " * (300 * 1024 * 1024))
    assert len(buffer.getvalue()) < 2 * 1024 * 1024
    with pytest.raises(MeshRejectedError):
        normalise_mesh("3mf", buffer.getvalue())


def test_triangles_past_the_cap_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(meshes, "MAX_MESH_TRIANGLES", 11)
    for kind, data in (
        ("stl", binary_stl(BOX)),
        ("stl", ascii_stl(BOX)),
        ("obj", obj_text(BOX)),
        ("3mf", part_mesh_3mf(BOX)),
    ):
        with pytest.raises(MeshRejectedError, match="11"):
            normalise_mesh(kind, data)  # type: ignore[arg-type]


def test_a_preview_is_a_png() -> None:
    stored = normalise_mesh("stl", binary_stl(BOX))
    assert preview_png("stl", stored.data).startswith(b"\x89PNG\r\n\x1a\n")


def test_the_store_keeps_a_mesh_with_its_triangles_and_bounds(
    tmp_path: object, pg_pool: PgPool
) -> None:
    from pathlib import Path

    store = AssetStore(Path(str(tmp_path)) / "assets", pg_pool)
    meta = store.put(binary_stl(BOX), "box.stl")
    assert (meta.kind, meta.triangles, meta.name) == ("stl", 12, "box.stl")
    assert meta.bounds == [-5.0, -10.0, -15.0, 5.0, 10.0, 15.0]
    assert store.get(meta.id) == meta
    assert store.blob_path(meta).name == f"{meta.id}.stl"
    with pytest.raises(AssetRejectedError):
        store.put(b"v 0 0 0\n", "nothing.obj")
