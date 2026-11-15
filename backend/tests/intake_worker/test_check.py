"""The check of a customer's staged file (#6133, spec 2026-11-13 §7): its kind by content,
the zip's central directory before anything inflates, and the read within the budget."""

from __future__ import annotations

import io
import struct
import zipfile
from pathlib import Path

import pytest
import trimesh

from scadbuddy.intake_worker import check as checks
from scadbuddy.intake_worker.check import Refused, check, check_zip, sniff
from scadbuddy.render.bambu3mf import stl_3mf
from scadbuddy.render.read_budget import ReadBudget


def box_stl(extents: tuple[float, float, float] = (20, 10, 5)) -> bytes:
    data = trimesh.creation.box(extents=extents).export(file_type="stl")
    assert isinstance(data, bytes)
    return data


def ascii_stl() -> bytes:
    data = trimesh.creation.box(extents=(4, 4, 4)).export(file_type="stl_ascii")
    return data.encode() if isinstance(data, str) else data


def write(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def zipped(
    tmp_path: Path, entries: dict[str, bytes], *, method: int = zipfile.ZIP_DEFLATED
) -> Path:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=method) as out:
        for name, data in entries.items():
            out.writestr(name, data)
    return write(tmp_path, "model.3mf", buffer.getvalue())


MINIMAL = {"[Content_Types].xml": b"<x/>", "_rels/.rels": b"<x/>", "3D/3dmodel.model": b"<x/>"}


def test_a_binary_stl_is_checked_and_previewed(tmp_path: Path) -> None:
    checked = check(write(tmp_path, "box.stl", box_stl()), "Box.STL")
    assert checked.facts.format == "stl"
    [plate] = checked.facts.plates
    assert plate.index == 1 and plate.triangles == 12 == checked.facts.triangles
    assert plate.size_mm == pytest.approx((20, 10, 5))
    assert checked.preview.startswith(b"\x89PNG\r\n\x1a\n")


def test_an_ascii_stl_is_an_stl(tmp_path: Path) -> None:
    assert sniff(write(tmp_path, "a.stl", ascii_stl()), "a.stl") == "stl"


def test_a_3mf_is_read_plate_by_plate(tmp_path: Path) -> None:
    checked = check(write(tmp_path, "m.3mf", stl_3mf(box_stl())), "m.3mf")
    assert checked.facts.format == "3mf"
    assert [plate.index for plate in checked.facts.plates] == [1]
    assert checked.facts.plates[0].colours


@pytest.mark.parametrize(
    ("name", "data", "reason"),
    [
        ("x.stl", b"GIF89a" + b"\x00" * 100, "not a 3MF or an STL"),
        ("x.3mf", box_stl(), "does not end in .stl"),
        ("x.stl", b"PK\x03\x04" + b"\x00" * 100, "does not end in .3mf"),
        ("x.stl", b"solid \xff\xfe binary junk", "not a 3MF or an STL"),
    ],
)
def test_a_file_is_judged_by_its_content(
    tmp_path: Path, name: str, data: bytes, reason: str
) -> None:
    with pytest.raises(Refused, match=reason):
        sniff(write(tmp_path, name, data), name)


def test_too_many_entries_are_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(checks, "MAX_ENTRIES", 3)
    path = zipped(tmp_path, {**MINIMAL, "extra.txt": b""})
    with pytest.raises(Refused, match="4 files"):
        check_zip(path)


@pytest.mark.parametrize("name", ["/etc/passwd", "../up.model", "3D\\back.model"])
def test_an_unsafe_name_is_refused(tmp_path: Path, name: str) -> None:
    """(A NUL never reaches the check: `zipfile` ends a name at its first NUL.)"""
    path = zipped(tmp_path, {**MINIMAL, name: b""})
    with pytest.raises(Refused, match="unsafe name"):
        check_zip(path)


def test_a_bomb_ratio_is_refused(tmp_path: Path) -> None:
    path = zipped(tmp_path, {**MINIMAL, "3D/huge.model": b"\x00" * (4 << 20)})
    with pytest.raises(Refused, match="compressed past"):
        check_zip(path)


def test_a_compression_other_than_deflate_is_refused(tmp_path: Path) -> None:
    path = zipped(tmp_path, MINIMAL, method=zipfile.ZIP_BZIP2)
    with pytest.raises(Refused, match="other than deflate"):
        check_zip(path)


def test_an_encrypted_entry_is_refused(tmp_path: Path) -> None:
    path = zipped(tmp_path, MINIMAL)
    raw = bytearray(path.read_bytes())
    # Set the encrypted bit (general purpose flag bit 0) in each central directory entry.
    at = 0
    while (at := raw.find(b"PK\x01\x02", at)) != -1:
        flags = struct.unpack_from("<H", raw, at + 8)[0]
        struct.pack_into("<H", raw, at + 8, flags | 0x1)
        at += 4
    path.write_bytes(bytes(raw))
    with pytest.raises(Refused, match="encrypted"):
        check_zip(path)


def test_a_3mf_without_its_parts_is_refused(tmp_path: Path) -> None:
    path = zipped(tmp_path, {"[Content_Types].xml": b"<x/>", "_rels/.rels": b"<x/>"})
    with pytest.raises(Refused, match="no model"):
        check_zip(path)


def test_a_zip_that_is_not_one_is_refused(tmp_path: Path) -> None:
    path = write(tmp_path, "m.3mf", b"PK\x03\x04" + b"\x00" * 200)
    with pytest.raises(Refused, match="not a readable zip"):
        check(path, "m.3mf")


def test_an_stl_past_the_triangle_budget_is_refused_before_it_loads(tmp_path: Path) -> None:
    path = write(tmp_path, "box.stl", box_stl())
    with pytest.raises(Refused, match="cannot be read"):
        check(path, "box.stl", ReadBudget(max_triangles=5))


def test_an_entry_never_inflates_past_its_declared_size(tmp_path: Path) -> None:
    """§7: the read inflates through `zipfile`, whose reader stops at the size the
    directory declares. Measured here: an entry that declares 10 bytes but inflates to a
    megabyte reads no more than 10, then fails its CRC."""
    path = zipped(tmp_path, {"3D/3dmodel.model": b"A" * (1 << 20)})
    raw = bytearray(path.read_bytes())
    for signature, offset in ((b"PK\x03\x04", 22), (b"PK\x01\x02", 24)):
        at = raw.find(signature)
        struct.pack_into("<I", raw, at + offset, 10)
    path.write_bytes(bytes(raw))
    taken = 0
    with (
        zipfile.ZipFile(path) as archive,
        archive.open("3D/3dmodel.model") as entry,
        pytest.raises(zipfile.BadZipFile, match="CRC"),
    ):
        while chunk := entry.read(4096):
            taken += len(chunk)
    assert taken <= 10
