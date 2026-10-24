"""What an attached file is, from its bytes, and the name it keeps (#4251, §6.2)."""

from __future__ import annotations

import pytest

from scadbuddy.orders.attachments import INLINE, SHOWN_IMAGES, clean_filename, sniff


@pytest.mark.parametrize(
    ("head", "name", "expected"),
    [
        (b"\x89PNG\r\n\x1a\n" + b"\0" * 8, "x.bin", ("image", "image/png")),
        (b"\xff\xd8\xff\xe0" + b"\0" * 12, "x", ("image", "image/jpeg")),
        (b"RIFF\0\0\0\0WEBPVP8 ", "x", ("image", "image/webp")),
        (b"GIF89a" + b"\0" * 10, "x", ("image", "image/gif")),
        (b"\0\0\0\x1cftypavif\0\0\0\0", "x", ("image", "image/avif")),
        # An iPhone's photo shares the container with MP4, and is a photo.
        (b"\0\0\0\x18ftypheic\0\0\0\0", "IMG.HEIC", ("image", "image/heic")),
        (b"\0\0\0\x18ftypmif1\0\0\0\0", "x", ("image", "image/heic")),
        (b"\0\0\0\x14ftypqt  \0\0\0\0", "x.mov", ("video", "video/quicktime")),
        (b"\0\0\0\x18ftypisom\0\0\0\0", "x", ("video", "video/mp4")),
        (b"\0\0\0\x18ftypM4A \0\0\0\0", "x.m4a", ("other", "audio/mp4")),
        (b"\x1a\x45\xdf\xa3" + b"\0" * 12, "x", ("video", "video/webm")),
        (b"%PDF-1.7\n" + b"\0" * 7, "x", ("document", "application/pdf")),
        # Anything else is `other`, typed by its name for display only.
        (b"<svg xmlns=...>", "logo.svg", ("other", "image/svg+xml")),
        (b"solid part\n", "part.stl", ("other", "model/stl")),
        (b"\0\1\2", "blob", ("other", "application/octet-stream")),
    ],
)
def test_sniff(head: bytes, name: str, expected: tuple[str, str]) -> None:
    assert sniff(head, name) == expected


def test_only_sniffed_types_are_inline_and_never_a_page() -> None:
    assert "image/svg+xml" not in INLINE
    assert "text/html" not in INLINE
    assert SHOWN_IMAGES <= INLINE


@pytest.mark.parametrize(
    ("sent", "kept"),
    [
        ("photo.jpg", "photo.jpg"),
        ("C:\\Users\\me\\Desktop\\photo.jpg", "photo.jpg"),
        ("../../etc/passwd", "passwd"),
        ("a\x00b\nc.png", "abc.png"),
        ("", "file"),
        ("..", "file"),
        ("x" * 300 + ".png", "x" * 251 + ".png"),
        ("y" * 300, "y" * 255),
    ],
)
def test_clean_filename(sent: str, kept: str) -> None:
    assert clean_filename(sent) == kept
