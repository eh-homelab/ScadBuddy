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
    assert digest == hashlib.sha256(data[:length]).hexdigest()


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
