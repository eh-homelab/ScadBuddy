from __future__ import annotations

import hashlib
import struct
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scadbuddy.camera.mp4 import init_span, segment_start

FIXTURE = Path(__file__).parent / "fixtures" / "seg.mp4"


def boxes(data: bytes) -> list[tuple[bytes, int]]:
    found, at = [], 0
    while at < len(data):
        size, kind = struct.unpack(">I4s", data[at : at + 8])
        found.append((kind, size))
        at += size
    return found


def test_init_span_of_fixture() -> None:
    data = FIXTURE.read_bytes()
    kinds = boxes(data)
    assert [k for k, _ in kinds[:3]] == [b"ftyp", b"moov", b"moof"]
    length, digest = init_span(FIXTURE)
    assert length == kinds[0][1] + kinds[1][1]
    ftyp = kinds[0][1]
    children = boxes(data[ftyp + 8 : length])
    assert b"udta" in [k for k, _ in children]
    kept, at = data[:ftyp], ftyp + 8
    for kind, size in children:
        if kind != b"udta":
            kept += data[at : at + size]
        at += size
    assert digest == hashlib.sha256(kept).hexdigest()


def box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def init(trak: bytes, mtxi: bytes) -> bytes:
    """An init as MediaMTX writes it: a ``udta`` with an ``mtxi`` box after the tracks."""
    moov = box(b"mvhd", b"\0" * 100) + box(b"trak", trak) + box(b"udta", box(b"mtxi", mtxi))
    return box(b"ftyp", b"iso5\0\0\0\1") + box(b"moov", moov) + box(b"moof", b"\0" * 8)


def test_init_digest_ignores_mediamtx_segment_info(tmp_path: Path) -> None:
    """MediaMTX v1.21.4 writes ``moov/udta/mtxi`` in each segment's init: its stream id, the
    segment's number, DTS and NTP time. So two segments of one stream share no whole init;
    the digest leaves ``udta`` out, and only a change of the codec setup changes it."""
    first, second, other = tmp_path / "1.mp4", tmp_path / "2.mp4", tmp_path / "3.mp4"
    first.write_bytes(init(b"avc1 640x480", b"\0" * 44))
    second.write_bytes(init(b"avc1 640x480", b"\1" * 44))
    other.write_bytes(init(b"avc1 320x240", b"\0" * 44))
    length, digest = init_span(first)
    assert length == first.stat().st_size - 16
    assert init_span(second) == (length, digest)
    assert init_span(other)[1] != digest


def test_init_span_rejects_non_mp4(tmp_path: Path) -> None:
    junk = tmp_path / "x.mp4"
    junk.write_bytes(b"not an mp4 file at all")
    with pytest.raises(ValueError):
        init_span(junk)


def test_init_span_rejects_missing_moov(tmp_path: Path) -> None:
    data = FIXTURE.read_bytes()
    ftyp = struct.unpack(">I", data[:4])[0]
    short = tmp_path / "x.mp4"
    short.write_bytes(data[:ftyp])
    with pytest.raises(ValueError):
        init_span(short)


def test_segment_start_parses_name() -> None:
    path = Path("/rec/camera/p1/2026-11-23_21-04-05-123456.mp4")
    assert segment_start(path) == datetime(2026, 11, 23, 21, 4, 5, 123456, tzinfo=UTC)
