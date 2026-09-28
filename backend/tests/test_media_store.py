"""The pieces of template media (#274) that need no app: typing by magic bytes,
reading ``model.json`` entries, the history's ignore rules."""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from scadbuddy.core.paths import DataPaths
from scadbuddy.library.catalogue import (
    Catalogue,
    MediaNotFoundError,
    MediaOrderError,
    ModelMeta,
    TooManyMediaError,
    meta_from_raw,
)
from scadbuddy.library.history import _gitignore_body
from scadbuddy.library.media import (
    LEGACY_ID,
    MAX_MEDIA_ITEMS,
    MEDIA_UPLOAD_PREFIX,
    MediaItem,
    MediaKind,
    StagedMedia,
    readable_media,
    sniff_kind,
)
from scadbuddy.render.provenance import source_version
from scadbuddy.render.solids import WRAPPER_PREFIX


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        (b"\x89PNG\r\n\x1a\n\x00\x00", ("image", "png", "image/png")),
        (b"\xff\xd8\xff\xdb\x00", ("image", "jpg", "image/jpeg")),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", ("image", "webp", "image/webp")),
        (b"\x00\x00\x00\x20ftypmp42\x00", ("video", "mp4", "video/mp4")),
        (b"\x1a\x45\xdf\xa3\x01\x00", ("video", "webm", "video/webm")),
    ],
)
def test_media_is_typed_by_its_magic_bytes(head: bytes, expected: tuple[str, str, str]) -> None:
    assert sniff_kind(head) == expected


@pytest.mark.parametrize(
    "head",
    [
        b"",
        b"hello world, not media",
        b"GIF89a\x00\x00",
        b"RIFF\x00\x00\x00\x00WAVEfmt ",
        b"<svg xmlns=",
    ],
)
def test_anything_else_is_not_media(head: bytes) -> None:
    assert sniff_kind(head) is None


def test_media_defaults_to_none() -> None:
    assert ModelMeta(name="x").media == []
    assert meta_from_raw({"media": None}, "x").media == []


def test_an_unreadable_entry_is_dropped_not_fatal() -> None:
    """As a bare library name is: a hand edit must not take the model off the list."""
    meta = meta_from_raw(
        {
            "media": [
                {"id": "good", "file": "good.png", "kind": "image"},
                {"id": "bad", "file": "../escape.png", "kind": "image"},
                {"id": "odd", "file": "odd.gif", "kind": "gif"},
                "not an entry",
                {"id": "..", "file": "x.png", "kind": "image"},
                {"id": "p", "file": "p.mp4", "kind": "video", "poster": "/etc/passwd"},
            ]
        },
        "x",
    )

    assert [item.id for item in meta.media] == ["good"]


def test_a_second_entry_with_a_taken_id_is_dropped() -> None:
    entries = [
        {"id": "a", "file": "a.png", "kind": "image"},
        {"id": "a", "file": "b.png", "kind": "image"},
    ]

    assert [item.file for item in readable_media(entries)] == ["a.png"]


@pytest.mark.parametrize("file", ["../x.png", "a/b.png", ".hidden.png", "", "x.png\n"])
def test_a_file_name_is_a_bare_name_inside_media(file: str) -> None:
    with pytest.raises(ValidationError):
        MediaItem(id="abc", file=file, kind="image")


def test_videos_are_kept_out_of_the_history_and_images_are_not() -> None:
    body = _gitignore_body(WRAPPER_PREFIX).splitlines()

    for pattern in (
        "*/media/*.mp4",
        "*/media/*.webm",
        "_builtin/*/media/*.mp4",
        "_builtin/*/media/*.webm",
    ):
        assert pattern in body
    assert not any("png" in line or "jpg" in line or "webp" in line for line in body)


def test_media_does_not_change_what_a_model_renders_from(tmp_path: Path) -> None:
    """A render's version must not move when a picture is added -- nor hash a video."""
    (tmp_path / "model.scad").write_text("cube(1);\n", encoding="utf-8")
    before = source_version(tmp_path)

    (tmp_path / "media").mkdir()
    (tmp_path / "media" / "abcdefabcdef.mp4").write_bytes(b"\x00" * 64)

    assert source_version(tmp_path) == before


# ── the catalogue's media, without history ────────────────────────────────────

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def _catalogue(tmp_path: Path) -> Catalogue:
    paths = DataPaths(tmp_path / "data")
    paths.ensure()
    catalogue = Catalogue(paths)
    catalogue.create("demo", "cube(1);\n", ModelMeta(name="Demo"))
    return catalogue


def _stage(catalogue: Catalogue, payload: bytes, kind: MediaKind, ext: str) -> StagedMedia:
    path = catalogue.paths.cache / f"{MEDIA_UPLOAD_PREFIX}{uuid.uuid4().hex}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return StagedMedia(path=path, kind=kind, extension=ext)


def test_a_new_model_holds_no_media_entry(tmp_path: Path) -> None:
    catalogue = _catalogue(tmp_path)

    assert "media" not in catalogue.read_raw_meta("demo")
    assert catalogue.list_media("demo") == []


def test_an_upload_is_moved_in_and_listed_last(tmp_path: Path) -> None:
    catalogue = _catalogue(tmp_path)
    staged = _stage(catalogue, PNG, "image", "png")

    catalogue.add_media("demo", staged, caption="Front")
    record = catalogue.add_media("demo", _stage(catalogue, b"\x1a\x45\xdf\xa3", "video", "webm"))

    assert not staged.path.exists()
    assert [(item.kind, item.caption) for item in record.media] == [
        ("image", "Front"),
        ("video", ""),
    ]
    assert record.media[0].file == f"{record.media[0].id}.png"
    assert (catalogue.media_dir("demo") / record.media[0].file).read_bytes() == PNG


def test_the_first_write_converts_the_legacy_thumbnail(tmp_path: Path) -> None:
    catalogue = _catalogue(tmp_path)
    catalogue.thumbnail_path("demo").write_bytes(PNG)
    assert [item.id for item in catalogue.list_media("demo")] == [LEGACY_ID]

    record = catalogue.set_caption("demo", LEGACY_ID, "Cover")

    [item] = record.media
    assert item.id != LEGACY_ID
    assert item.caption == "Cover"
    assert not catalogue.thumbnail_path("demo").exists()
    assert (catalogue.media_dir("demo") / item.file).read_bytes() == PNG


def test_a_reorder_must_be_a_permutation(tmp_path: Path) -> None:
    catalogue = _catalogue(tmp_path)
    catalogue.add_media("demo", _stage(catalogue, PNG, "image", "png"))
    ids = [item.id for item in catalogue.list_media("demo")]

    with pytest.raises(MediaOrderError):
        catalogue.reorder("demo", [*ids, *ids])
    with pytest.raises(MediaNotFoundError):
        catalogue.remove_media("demo", "abcdefabcdef")


def test_the_item_limit_is_enforced(tmp_path: Path) -> None:
    catalogue = _catalogue(tmp_path)
    raw = catalogue.read_raw_meta("demo")
    raw["media"] = [
        {"id": f"i{index}", "file": f"i{index}.png", "kind": "image"}
        for index in range(MAX_MEDIA_ITEMS)
    ]
    catalogue.write_raw_meta("demo", raw)
    staged = _stage(catalogue, PNG, "image", "png")

    with pytest.raises(TooManyMediaError):
        catalogue.add_media("demo", staged)
    assert staged.path.exists()


def test_a_crashed_uploads_staging_is_swept_once_it_is_old(tmp_path: Path) -> None:
    catalogue = _catalogue(tmp_path)
    fresh = _stage(catalogue, PNG, "image", "png")
    stale = _stage(catalogue, PNG, "image", "png")
    old = time.time() - 2 * 3600
    os.utime(stale.path, (old, old))

    removed = catalogue.sweep_duplicate_staging()

    assert removed == [stale.path.name]
    assert fresh.path.exists()
