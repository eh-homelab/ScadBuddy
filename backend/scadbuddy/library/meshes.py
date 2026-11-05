"""STL, 3MF and OBJ files for `// file` parameters (#959): read, checked and rewritten.

The asset store keeps what it can vouch for, never the bytes as sent (`assets.py`): an
SVG is re-serialised, a PNG re-encoded, and a mesh is read by a bounded reader and
written back in one canonical form per kind. The stored bytes, and so the asset id, are
that form, and a render's `import()` reads them. Anything the readers do not take is a
:class:`MeshRejectedError` naming why.

Every kind is held to :data:`MAX_MESH_TRIANGLES` and :data:`MAX_MESH_VERTICES`, finite
coordinates no further than :data:`MAX_COORDINATE` from the origin, and at least one
triangle; the bytes read are the upload's and the fetch's 8 MB (`MAX_ASSET_BYTES`).

- **STL**: binary when its length is exactly ``84 + 50 * count`` for the count its
  header states (a binary header may begin ``solid``, so the length decides); else
  ASCII, sized against the triangles it could hold before it is parsed
  (`render/objects3mf.py` `check_stl`). Stored binary, its 80-byte header zeroed and
  every normal and attribute zero: nothing of the sender's survives.
- **OBJ**: UTF-8 text read here line by line: ``v`` and ``f`` (``a``, ``a/b``, ``a//c``,
  ``a/b/c``, negative indices counted back), each face naming vertices already defined,
  a polygon fanned into triangles. ``vt``, ``vn``, ``o``, ``g``, ``s``, ``l``,
  ``usemtl`` and comments are skipped; ``mtllib`` names another file, which an
  `import()` never opens, and is dropped with them. Stored as ``v`` and ``f`` lines.
- **3MF**: a zip, held to :data:`MAX_3MF_ENTRIES` entries, each declaring at most
  :data:`MAX_3MF_ENTRY_BYTES` inflated (Python's zip reader stops an entry at what it
  declares, so a lying header buys nothing) and :data:`MAX_3MF_TOTAL_BYTES` together, no
  entry inflating past :data:`MAX_3MF_RATIO` times its compressed size, no encrypted
  entry and no name that is absolute or climbs out with ``..``. Inside those, the
  library 3MF reader (`render/objects3mf.py` `read_objects`, components, transforms and
  the production extension's parts included) reads its build, which must place exactly
  one object; its printed parts (not a negative part, a modifier or a support volume)
  become one mesh. Stored as a fresh 3MF of that mesh alone: no thumbnails, metadata or
  slicer settings, which `import()` never reads.

A mesh's preview (:func:`preview_png`) is the plate thumbnail rasteriser's iso view,
drawn from the stored, canonical bytes.
"""

from __future__ import annotations

import io
import struct
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Final, Literal

import numpy as np
import trimesh

from scadbuddy.render.bambu3mf import (
    CORE_NS,
    MODEL_CONTENT_TYPE,
    MODEL_RELATIONSHIP,
    RELS_CONTENT_TYPE,
    ROOT_MODEL_NAME,
    ZIP_TIMESTAMP,
)
from scadbuddy.render.objects3mf import UnreadableObjectsError, check_stl, read_objects
from scadbuddy.render.read_budget import ReadBudget
from scadbuddy.render.split import ColourPart
from scadbuddy.render.thumbnail import render_view

MeshKind = Literal["stl", "3mf", "obj"]
MESH_KINDS: Final[tuple[MeshKind, ...]] = ("stl", "3mf", "obj")

#: A render reads the mesh on every colour pass; this bounds that work whatever the
#: encoding. A binary STL of 8 MB holds about 167,000.
MAX_MESH_TRIANGLES = 500_000
#: Three per triangle with nothing shared.
MAX_MESH_VERTICES = 3 * MAX_MESH_TRIANGLES
#: A kilometre either way, in millimetres: further is a unit mistake, not a part.
MAX_COORDINATE = 1_000_000.0
MAX_3MF_ENTRIES = 64
MAX_3MF_ENTRY_BYTES = 64 * 1024 * 1024
MAX_3MF_TOTAL_BYTES = 128 * 1024 * 1024
#: Deflate tops out near 1032:1, and a mesh's XML compresses about 5:1 to 10:1.
MAX_3MF_RATIO = 100
#: An entry this small is not worth a ratio check: a tiny file compresses oddly.
_RATIO_FLOOR = 64 * 1024
#: What the preview is drawn as.
PREVIEW_SIZE = 256
_PREVIEW_COLOUR = "#B8B8B8"

_STL_HEADER = 84
_STL_TRIANGLE = 50


class MeshRejectedError(ValueError):
    """The file is not a mesh this store keeps."""


@dataclass(frozen=True)
class StoredMesh:
    data: bytes
    triangles: int
    #: min x, y, z, then max x, y, z, in the file's units.
    bounds: list[float]


def _checked(vertices: np.ndarray, faces: np.ndarray, kind: str) -> tuple[np.ndarray, np.ndarray]:
    vertices = np.asarray(vertices, dtype=np.float64).reshape(-1, 3)
    faces = np.asarray(faces, dtype=np.int64).reshape(-1, 3)
    if len(faces) == 0:
        raise MeshRejectedError(f"the {kind} holds no triangles")
    if len(faces) > MAX_MESH_TRIANGLES:
        raise MeshRejectedError(
            f"the {kind} holds {len(faces):,} triangles; at most {MAX_MESH_TRIANGLES:,} are kept"
        )
    if len(vertices) > MAX_MESH_VERTICES:
        raise MeshRejectedError(
            f"the {kind} holds {len(vertices):,} vertices; at most {MAX_MESH_VERTICES:,} are kept"
        )
    if not np.isfinite(vertices).all():
        raise MeshRejectedError(f"the {kind} has a coordinate that is not a finite number")
    if np.abs(vertices).max(initial=0.0) > MAX_COORDINATE:
        raise MeshRejectedError(
            f"the {kind} reaches past {MAX_COORDINATE:,.0f} mm from the origin; check its units"
        )
    if faces.min() < 0 or faces.max() >= len(vertices):
        raise MeshRejectedError(f"a triangle of the {kind} names a vertex it lacks")
    return vertices, faces


def _bounds(vertices: np.ndarray, faces: np.ndarray) -> list[float]:
    used = vertices[np.unique(faces)]
    return [float(v) for v in (*used.min(axis=0), *used.max(axis=0))]


# ── STL ──────────────────────────────────────────────────────────────────────


def _read_stl(data: bytes) -> tuple[np.ndarray, np.ndarray]:
    budget = ReadBudget(max_triangles=MAX_MESH_TRIANGLES)
    try:
        check_stl(io.BytesIO(data), budget)
    except UnreadableObjectsError as error:
        raise MeshRejectedError(f"the STL holds more than {MAX_MESH_TRIANGLES:,} triangles") from (
            error
        )
    count = struct.unpack("<I", data[80:84])[0] if len(data) >= _STL_HEADER else -1
    if count >= 0 and len(data) == _STL_HEADER + _STL_TRIANGLE * count:
        records = np.frombuffer(
            data,
            dtype=np.dtype([("normal", "<f4", 3), ("corners", "<f4", (3, 3)), ("attr", "<u2")]),
            offset=_STL_HEADER,
            count=count,
        )
        corners = records["corners"].astype(np.float64).reshape(-1, 3)
        return corners, np.arange(len(corners)).reshape(-1, 3)
    return _read_ascii_stl(data)


def _read_ascii_stl(data: bytes) -> tuple[np.ndarray, np.ndarray]:
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        raise MeshRejectedError("the file is neither a binary nor a text STL") from None
    words = text.split()
    if not words or words[0].lower() != "solid":
        raise MeshRejectedError("the file is neither a binary nor a text STL")
    corners: list[tuple[float, float, float]] = []
    i = 0
    while i < len(words):
        if words[i].lower() == "vertex":
            try:
                corners.append((float(words[i + 1]), float(words[i + 2]), float(words[i + 3])))
            except (IndexError, ValueError):
                raise MeshRejectedError("a vertex of the text STL is not three numbers") from None
            i += 4
        else:
            i += 1
    if len(corners) % 3:
        raise MeshRejectedError("the text STL has a facet without three vertices")
    if not corners:
        raise MeshRejectedError("the text STL holds no triangles")
    vertices = np.array(corners, dtype=np.float64)
    return vertices, np.arange(len(vertices)).reshape(-1, 3)


def _write_stl(vertices: np.ndarray, faces: np.ndarray) -> bytes:
    records = np.zeros(
        len(faces),
        dtype=np.dtype([("normal", "<f4", 3), ("corners", "<f4", (3, 3)), ("attr", "<u2")]),
    )
    records["corners"] = vertices[faces].astype("<f4")
    return bytes(80) + struct.pack("<I", len(faces)) + records.tobytes()


# ── OBJ ──────────────────────────────────────────────────────────────────────

_OBJ_SKIPPED = frozenset({"vt", "vn", "vp", "o", "g", "s", "l", "usemtl", "mtllib"})


def _read_obj(data: bytes) -> tuple[np.ndarray, np.ndarray]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise MeshRejectedError("the OBJ is not UTF-8 text") from None
    vertices: list[tuple[float, float, float]] = []
    faces: list[tuple[int, int, int]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        words = line.split("#", 1)[0].split()
        if not words or words[0] in _OBJ_SKIPPED:
            continue
        if words[0] == "v":
            if len(words) not in (4, 5, 7):
                raise MeshRejectedError(f"line {number} of the OBJ is a vertex without x, y, z")
            try:
                x, y, z = (float(w) for w in words[1:4])
            except ValueError:
                raise MeshRejectedError(
                    f"line {number} of the OBJ has a vertex that is not numbers"
                ) from None
            if len(vertices) == MAX_MESH_VERTICES:
                raise MeshRejectedError(f"the OBJ holds more than {MAX_MESH_VERTICES:,} vertices")
            vertices.append((x, y, z))
        elif words[0] == "f":
            corners = [_obj_index(w, len(vertices), number) for w in words[1:]]
            if len(corners) < 3:
                raise MeshRejectedError(
                    f"line {number} of the OBJ is a face of fewer than three vertices"
                )
            for k in range(1, len(corners) - 1):
                if len(faces) == MAX_MESH_TRIANGLES:
                    raise MeshRejectedError(
                        f"the OBJ holds more than {MAX_MESH_TRIANGLES:,} triangles"
                    )
                faces.append((corners[0], corners[k], corners[k + 1]))
        else:
            raise MeshRejectedError(
                f"line {number} of the OBJ is a {words[0][:20]!r} statement, which is not read"
            )
    return np.array(vertices, dtype=np.float64).reshape(-1, 3), np.array(faces, dtype=np.int64)


def _obj_index(word: str, defined: int, number: int) -> int:
    try:
        index = int(word.split("/", 1)[0])
    except ValueError:
        raise MeshRejectedError(
            f"line {number} of the OBJ names a vertex that is not a number"
        ) from None
    resolved = index - 1 if index > 0 else defined + index
    if index == 0 or not 0 <= resolved < defined:
        raise MeshRejectedError(f"line {number} of the OBJ names a vertex not defined before it")
    return resolved


def _number(value: float) -> str:
    text = repr(float(value))
    return text[:-2] if text.endswith(".0") else text


def _write_obj(vertices: np.ndarray, faces: np.ndarray) -> bytes:
    lines = [f"v {_number(x)} {_number(y)} {_number(z)}" for x, y, z in vertices.tolist()]
    lines += [f"f {a + 1} {b + 1} {c + 1}" for a, b, c in faces.tolist()]
    return ("\n".join(lines) + "\n").encode()


# ── 3MF ──────────────────────────────────────────────────────────────────────


def _check_archive(data: bytes) -> None:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, ValueError):
        raise MeshRejectedError("the file is not a 3MF: it is not a readable zip") from None
    with archive:
        entries = archive.infolist()
        if len(entries) > MAX_3MF_ENTRIES:
            raise MeshRejectedError(
                f"the 3MF holds {len(entries)} files; at most {MAX_3MF_ENTRIES} are read"
            )
        names: set[str] = set()
        total = 0
        for entry in entries:
            path = PurePosixPath(entry.filename)
            if entry.filename.startswith("/") or "\\" in entry.filename or ".." in path.parts:
                raise MeshRejectedError("the 3MF names a file outside itself")
            if entry.filename in names:
                raise MeshRejectedError("the 3MF holds two files of one name")
            names.add(entry.filename)
            if entry.flag_bits & 0x1:
                raise MeshRejectedError("the 3MF holds an encrypted file")
            if entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise MeshRejectedError("the 3MF holds a file compressed in a way that is not read")
            if entry.file_size > MAX_3MF_ENTRY_BYTES:
                raise MeshRejectedError(
                    f"a file in the 3MF inflates to {entry.file_size:,} bytes; at most "
                    f"{MAX_3MF_ENTRY_BYTES:,} are read"
                )
            if entry.file_size > _RATIO_FLOOR and entry.file_size > MAX_3MF_RATIO * max(
                entry.compress_size, 1
            ):
                raise MeshRejectedError("a file in the 3MF inflates more than any mesh does")
            total += entry.file_size
        if total > MAX_3MF_TOTAL_BYTES:
            raise MeshRejectedError(
                f"the 3MF inflates to {total:,} bytes; at most {MAX_3MF_TOTAL_BYTES:,} are read"
            )


def _read_3mf(data: bytes) -> tuple[np.ndarray, np.ndarray]:
    _check_archive(data)
    try:
        objects = read_objects(data, ReadBudget(max_triangles=MAX_MESH_TRIANGLES))
    except UnreadableObjectsError as error:
        raise MeshRejectedError(f"the 3MF cannot be read: {error}") from None
    if len(objects) != 1 or objects[0].count != 1:
        placed = sum(o.count for o in objects)
        raise MeshRejectedError(
            f"the 3MF places {placed} objects; a file parameter takes one object: "
            "export the one you want on its own"
        )
    parts = [p.mesh for p in objects[0].parts if not p.negative]
    if not parts:
        raise MeshRejectedError("the 3MF's object has nothing that prints")
    vertices: list[np.ndarray] = []
    faces: list[np.ndarray] = []
    offset = 0
    for part in parts:
        vertices.append(np.asarray(part.vertices))
        faces.append(np.asarray(part.faces) + offset)
        offset += len(part.vertices)
    return np.concatenate(vertices), np.concatenate(faces)


_TYPES = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    f'<Default Extension="rels" ContentType="{RELS_CONTENT_TYPE}"/>'
    f'<Default Extension="model" ContentType="{MODEL_CONTENT_TYPE}"/></Types>'
)
_RELS = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    f'<Relationship Target="/{ROOT_MODEL_NAME}" Id="rel0" Type="{MODEL_RELATIONSHIP}"/>'
    "</Relationships>"
)


def _write_3mf(vertices: np.ndarray, faces: np.ndarray) -> bytes:
    points = "".join(
        f'<vertex x="{_number(x)}" y="{_number(y)}" z="{_number(z)}"/>'
        for x, y, z in vertices.tolist()
    )
    triangles = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in faces.tolist())
    model = (
        f'<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xmlns="{CORE_NS}">'
        f'<resources><object id="1" type="model"><mesh><vertices>{points}</vertices>'
        f"<triangles>{triangles}</triangles></mesh></object></resources>"
        '<build><item objectid="1"/></build></model>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, text in (
            ("[Content_Types].xml", _TYPES),
            ("_rels/.rels", _RELS),
            (ROOT_MODEL_NAME, model),
        ):
            info = zipfile.ZipInfo(name, date_time=ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, text)
    return buffer.getvalue()


# ── the kinds ────────────────────────────────────────────────────────────────

_READERS = {"stl": _read_stl, "obj": _read_obj, "3mf": _read_3mf}
_WRITERS = {"stl": _write_stl, "obj": _write_obj, "3mf": _write_3mf}


def normalise_mesh(kind: MeshKind, data: bytes) -> StoredMesh:
    """``data``, a ``kind`` file, read, checked and written in its canonical form."""
    vertices, faces = _checked(*_READERS[kind](data), kind.upper())
    return StoredMesh(
        data=_WRITERS[kind](vertices, faces),
        triangles=len(faces),
        bounds=_bounds(vertices, faces),
    )


def preview_png(kind: MeshKind, stored: bytes) -> bytes:
    """An iso view of a stored mesh, ``PREVIEW_SIZE`` square."""
    vertices, faces = _READERS[kind](stored)
    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
    return render_view([ColourPart(1, "mesh", _PREVIEW_COLOUR, mesh)], "iso", size=PREVIEW_SIZE)


def looks_like_mesh(data: bytes) -> MeshKind | None:
    """Which mesh kind ``data`` is, by its bytes, or ``None``; read in full only when
    stored (:func:`normalise_mesh`)."""
    if data.startswith(b"PK\x03\x04"):
        return "3mf"
    if len(data) >= _STL_HEADER:
        count = struct.unpack("<I", data[80:84])[0]
        if len(data) == _STL_HEADER + _STL_TRIANGLE * count:
            return "stl"
    head = data[:4096].lstrip(b"\xef\xbb\xbf \t\r\n")
    if head[:5].lower() == b"solid":
        return "stl"
    if any(line.split()[:1] == [b"v"] for line in head.splitlines()):
        return "obj"
    return None
