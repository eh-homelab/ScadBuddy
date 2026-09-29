"""The pieces of template media (#274) below the API: typing by magic bytes,
reading a bundled ``model.json``'s entries, the history's ignore rules, the
`template_media` store and the catalogue on top of it."""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

import pytest
from pydantic import ValidationError

from scadbuddy.core.paths import DataPaths
from scadbuddy.library.catalogue import (
    Catalogue,
    MediaNotFoundError,
    MediaOrderError,
    MediaUnavailableError,
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
from scadbuddy.library.media_store import PostgresMediaStore
from scadbuddy.render.pg_store import PostgresJobStore
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


# ── the `template_media` store and the catalogue on it ─────────────────────────

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


@pytest.fixture
def data(tmp_path: Path) -> DataPaths:
    paths = DataPaths(tmp_path / "data")
    paths.ensure()
    return paths


@pytest.fixture
def store(pg_conninfo: str, data: DataPaths) -> Iterator[PostgresMediaStore]:
    """Opened as the app opens it: through the job store, which runs the migrations."""
    jobs = PostgresJobStore(pg_conninfo, data, pool_size=2)
    jobs.open()
    try:
        yield PostgresMediaStore(jobs.pool)
    finally:
        jobs.close()


@pytest.fixture
def catalogue(data: DataPaths, store: PostgresMediaStore) -> Catalogue:
    catalogue = Catalogue(data, media_store=store, wrapper_prefix=WRAPPER_PREFIX)
    catalogue.create("demo", "cube(1);\n", ModelMeta(name="Demo"))
    return catalogue


def _stage(catalogue: Catalogue, payload: bytes, kind: MediaKind, ext: str) -> StagedMedia:
    path = catalogue.paths.cache / f"{MEDIA_UPLOAD_PREFIX}{uuid.uuid4().hex}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return StagedMedia(path=path, kind=kind, extension=ext)


def _item(item_id: str, caption: str = "") -> MediaItem:
    return MediaItem(id=item_id, file=f"{item_id}.png", kind="image", caption=caption)


@pytest.mark.requires_postgres
def test_the_store_keeps_each_templates_list_in_order(store: PostgresMediaStore) -> None:
    store.replace("a", [_item("one", "First"), _item("two")])
    store.replace("b", [_item("one")])

    store.replace("a", [_item("two"), _item("one", "First")])

    assert [(item.id, item.caption) for item in store.items("a")] == [
        ("two", ""),
        ("one", "First"),
    ]
    assert [item.id for item in store.items("b")] == ["one"]
    store.delete("a")
    assert store.items("a") == []
    assert store.items("nothing") == []


@pytest.mark.requires_postgres
def test_a_failed_replace_leaves_the_list_as_it_was(store: PostgresMediaStore) -> None:
    store.replace("a", [_item("one")])

    with pytest.raises(Exception, match="template_media"):
        store.replace("a", [_item("two"), _item("two")])

    assert [item.id for item in store.items("a")] == ["one"]


@pytest.mark.requires_postgres
def test_a_new_model_holds_no_media(catalogue: Catalogue, store: PostgresMediaStore) -> None:
    assert "media" not in catalogue.read_raw_meta("demo")
    assert catalogue.list_media("demo") == []
    assert store.items("demo") == []


@pytest.mark.requires_postgres
def test_an_upload_is_moved_in_and_listed_last(
    catalogue: Catalogue, store: PostgresMediaStore
) -> None:
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
    # The list is rows, not model.json.
    assert [item.id for item in store.items("demo")] == [item.id for item in record.media]
    assert "media" not in catalogue.read_raw_meta("demo")


@pytest.mark.requires_postgres
def test_a_file_whose_row_cannot_be_written_is_removed(
    catalogue: Catalogue, store: PostgresMediaStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(template_id: str, items: list[MediaItem]) -> None:
        raise RuntimeError("the database is gone")

    monkeypatch.setattr(store, "replace", refuse)
    staged = _stage(catalogue, PNG, "image", "png")

    with pytest.raises(RuntimeError):
        catalogue.add_media("demo", staged)

    assert not catalogue.media_dir("demo").is_dir() or not any(
        catalogue.media_dir("demo").iterdir()
    )


@pytest.mark.requires_postgres
def test_a_file_with_no_row_is_ignored(catalogue: Catalogue) -> None:
    catalogue.media_dir("demo").mkdir()
    (catalogue.media_dir("demo") / "abcdefabcdef.png").write_bytes(PNG)

    assert catalogue.list_media("demo") == []


@pytest.mark.requires_postgres
def test_the_first_write_converts_the_legacy_thumbnail(catalogue: Catalogue) -> None:
    catalogue.thumbnail_path("demo").write_bytes(PNG)
    assert [item.id for item in catalogue.list_media("demo")] == [LEGACY_ID]

    record = catalogue.set_caption("demo", LEGACY_ID, "Cover")

    [item] = record.media
    assert item.id != LEGACY_ID
    assert item.caption == "Cover"
    assert not catalogue.thumbnail_path("demo").exists()
    assert (catalogue.media_dir("demo") / item.file).read_bytes() == PNG


@pytest.mark.requires_postgres
def test_a_failed_conversion_puts_the_legacy_thumbnail_back(
    catalogue: Catalogue, store: PostgresMediaStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalogue.thumbnail_path("demo").write_bytes(PNG)

    def refuse(template_id: str, items: list[MediaItem]) -> None:
        raise RuntimeError("the database is gone")

    monkeypatch.setattr(store, "replace", refuse)
    with pytest.raises(RuntimeError):
        catalogue.set_caption("demo", LEGACY_ID, "Cover")

    assert catalogue.thumbnail_path("demo").read_bytes() == PNG
    assert [item.id for item in catalogue.list_media("demo")] == [LEGACY_ID]


@pytest.mark.requires_postgres
def test_a_reorder_must_be_a_permutation(catalogue: Catalogue) -> None:
    catalogue.add_media("demo", _stage(catalogue, PNG, "image", "png"))
    ids = [item.id for item in catalogue.list_media("demo")]

    with pytest.raises(MediaOrderError):
        catalogue.reorder("demo", [*ids, *ids])
    with pytest.raises(MediaNotFoundError):
        catalogue.remove_media("demo", "abcdefabcdef")


@pytest.mark.requires_postgres
def test_the_item_limit_is_enforced(catalogue: Catalogue, store: PostgresMediaStore) -> None:
    store.replace("demo", [_item(f"i{index}") for index in range(MAX_MEDIA_ITEMS)])
    staged = _stage(catalogue, PNG, "image", "png")

    with pytest.raises(TooManyMediaError):
        catalogue.add_media("demo", staged)
    assert staged.path.exists()


class _CountingStore:
    """The store, counting its queries."""

    def __init__(self, store: PostgresMediaStore) -> None:
        self.store = store
        self.queries: list[str] = []

    def items(self, template_id: str) -> list[MediaItem]:
        self.queries.append("items")
        return self.store.items(template_id)

    def items_for(self, template_ids: Sequence[str]) -> dict[str, list[MediaItem]]:
        self.queries.append("items_for")
        return self.store.items_for(template_ids)

    def replace(self, template_id: str, items: list[MediaItem]) -> None:
        self.store.replace(template_id, items)

    def delete(self, template_id: str) -> None:
        self.store.delete(template_id)

    def cover(self, template_id: str) -> str | None:
        self.queries.append("cover")
        return self.store.cover(template_id)

    def covers_for(self, template_ids: Sequence[str]) -> dict[str, str]:
        self.queries.append("covers_for")
        return self.store.covers_for(template_ids)

    def set_cover(self, template_id: str, item_id: str | None) -> None:
        self.store.set_cover(template_id, item_id)


@pytest.mark.requires_postgres
def test_the_store_reads_many_templates_in_one_query(store: PostgresMediaStore) -> None:
    store.replace("a", [_item("one"), _item("two")])
    store.replace("b", [_item("three")])

    listed = store.items_for(["a", "b", "c"])

    assert {key: [item.id for item in value] for key, value in listed.items()} == {
        "a": ["one", "two"],
        "b": ["three"],
    }
    assert store.items_for([]) == {}


@pytest.mark.requires_postgres
def test_the_listing_reads_every_templates_media_in_one_query(
    data: DataPaths, store: PostgresMediaStore
) -> None:
    """ONE media query for the page, not one per model, as with the git walk."""
    counting = _CountingStore(store)
    catalogue = Catalogue(data, media_store=counting, wrapper_prefix=WRAPPER_PREFIX)
    for slug in ("one", "two", "three"):
        catalogue.create(slug, "cube(1);\n", ModelMeta(name=slug))
    catalogue.add_media("two", _stage(catalogue, PNG, "image", "png"), caption="Two")
    counting.queries.clear()

    records = {record.slug: record for record in catalogue.list_models()}

    assert counting.queries == ["items_for"]
    assert [item.caption for item in records["two"].media] == ["Two"]
    assert records["one"].media == []


@pytest.mark.requires_postgres
def test_the_listing_reads_the_built_ins_covers_in_one_more_query(
    data: DataPaths, store: PostgresMediaStore
) -> None:
    (data.builtins / "keychain").mkdir(parents=True)
    (data.builtins / "keychain" / "model.scad").write_text("cube(1);\n", encoding="utf-8")
    (data.builtins / "tag").mkdir(parents=True)
    (data.builtins / "tag" / "model.scad").write_text("cube(1);\n", encoding="utf-8")
    counting = _CountingStore(store)
    catalogue = Catalogue(data, media_store=counting, wrapper_prefix=WRAPPER_PREFIX)
    catalogue.create("mine", "cube(1);\n", ModelMeta(name="Mine"))
    for slug in ("builtin:keychain", "builtin:tag"):
        catalogue.add_media(slug, _stage(catalogue, PNG, "image", "png"))
    counting.queries.clear()

    catalogue.list_models()

    assert counting.queries == ["items_for", "covers_for"]


@pytest.mark.requires_postgres
def test_the_store_keeps_one_cover_per_template(store: PostgresMediaStore) -> None:
    store.set_cover("builtin:a", "one")
    store.set_cover("builtin:a", "two")
    store.set_cover("builtin:b", "three")

    assert store.cover("builtin:a") == "two"
    assert store.covers_for(["builtin:a", "builtin:b", "builtin:c"]) == {
        "builtin:a": "two",
        "builtin:b": "three",
    }
    store.set_cover("builtin:b", None)
    assert store.cover("builtin:b") is None
    store.delete("builtin:a")
    assert store.cover("builtin:a") is None
    assert store.covers_for([]) == {}


@pytest.fixture
def builtin(data: DataPaths, catalogue: Catalogue) -> str:
    """A built-in shipping one image in its bundled model.json, and a thumbnail."""
    directory = data.builtins / "keychain"
    (directory / "media").mkdir(parents=True)
    (directory / "model.scad").write_text("cube(1);\n", encoding="utf-8")
    (directory / "media" / "front.png").write_bytes(PNG)
    (directory / "model.json").write_text(
        '{"name": "Keychain", "media": [{"id": "front", "file": "front.png", "kind": "image"}]}',
        encoding="utf-8",
    )
    return "builtin:keychain"


@pytest.mark.requires_postgres
def test_media_added_to_a_built_in_follows_what_it_ships(
    catalogue: Catalogue, builtin: str, store: PostgresMediaStore, data: DataPaths
) -> None:
    shipped_tree = sorted(p.name for p in data.model_dir(builtin).rglob("*"))

    record = catalogue.add_media(builtin, _stage(catalogue, PNG, "image", "png"), caption="Mine")

    front, added = record.media
    assert (front.id, front.readonly) == ("front", True)
    assert (added.caption, added.readonly, added.missing) == ("Mine", False, False)
    assert record.media_cover is None
    # In the data directory, never in the image's mirror.
    assert (data.builtin_media_dir(builtin) / added.file).read_bytes() == PNG
    assert sorted(p.name for p in data.model_dir(builtin).rglob("*")) == shipped_tree
    assert [item.id for item in store.items(builtin)] == [added.id]
    _, path = catalogue.media_item(builtin, added.id)
    assert path == data.builtin_media_dir(builtin) / added.file


@pytest.mark.requires_postgres
def test_what_a_built_in_ships_cannot_change(catalogue: Catalogue, builtin: str) -> None:
    from scadbuddy.library.catalogue import MediaReadOnlyError

    with pytest.raises(MediaReadOnlyError):
        catalogue.set_caption(builtin, "front", "x")
    with pytest.raises(MediaReadOnlyError):
        catalogue.remove_media(builtin, "front")
    assert [(item.id, item.caption) for item in catalogue.list_media(builtin)] == [("front", "")]


@pytest.mark.requires_postgres
def test_a_built_ins_added_media_is_captioned_reordered_and_removed(
    catalogue: Catalogue, builtin: str, data: DataPaths
) -> None:
    one = catalogue.add_media(builtin, _stage(catalogue, PNG, "image", "png")).media[1]
    two = catalogue.add_media(builtin, _stage(catalogue, PNG, "image", "png")).media[2]

    catalogue.set_caption(builtin, two.id, "Second")
    # A shipped id in the order is passed over: it keeps its place.
    record = catalogue.reorder(builtin, [two.id, "front", one.id])
    assert [(item.id, item.caption) for item in record.media] == [
        ("front", ""),
        (two.id, "Second"),
        (one.id, ""),
    ]
    with pytest.raises(MediaOrderError):
        catalogue.reorder(builtin, [two.id])

    record = catalogue.remove_media(builtin, two.id)

    assert [item.id for item in record.media] == ["front", one.id]
    assert not (data.builtin_media_dir(builtin) / two.file).exists()


@pytest.mark.requires_postgres
def test_a_built_ins_cover_is_a_choice_of_its_own(
    catalogue: Catalogue, builtin: str, store: PostgresMediaStore
) -> None:
    added = catalogue.add_media(builtin, _stage(catalogue, PNG, "image", "png")).media[1]

    record = catalogue.set_cover(builtin, added.id)

    assert [item.id for item in record.media] == [added.id, "front"]
    assert record.media_cover == added.id
    assert catalogue.thumbnail(builtin) is not None
    # The first shipped item is the cover without a choice.
    record = catalogue.set_cover(builtin, "front")
    assert record.media_cover is None
    assert store.cover(builtin) is None
    with pytest.raises(MediaNotFoundError):
        catalogue.set_cover(builtin, "abcdefabcdef")

    catalogue.set_cover(builtin, added.id)
    record = catalogue.remove_media(builtin, added.id)

    assert [item.id for item in record.media] == ["front"]
    assert store.cover(builtin) is None


@pytest.mark.requires_postgres
def test_a_cover_of_mine_is_moved_to_the_front(catalogue: Catalogue) -> None:
    first = catalogue.add_media("demo", _stage(catalogue, PNG, "image", "png")).media[0]
    second = catalogue.add_media("demo", _stage(catalogue, PNG, "image", "png")).media[1]

    record = catalogue.set_cover("demo", second.id)

    assert [item.id for item in record.media] == [second.id, first.id]
    assert record.media_cover is None
    with pytest.raises(MediaOrderError):
        catalogue.set_cover("demo", None)


@pytest.mark.requires_postgres
def test_a_built_ins_limit_counts_what_it_ships(
    catalogue: Catalogue, builtin: str, store: PostgresMediaStore
) -> None:
    store.replace(builtin, [_item(f"i{index}") for index in range(MAX_MEDIA_ITEMS - 1)])

    with pytest.raises(TooManyMediaError):
        catalogue.add_media(builtin, _stage(catalogue, PNG, "image", "png"))


@pytest.mark.requires_postgres
def test_a_delete_removes_the_templates_rows(
    catalogue: Catalogue, store: PostgresMediaStore
) -> None:
    catalogue.add_media("demo", _stage(catalogue, PNG, "image", "png"))

    catalogue.delete("demo")

    assert store.items("demo") == []


# ── without a database ────────────────────────────────────────────────────────


def test_without_a_database_only_the_legacy_thumbnail_is_listed(data: DataPaths) -> None:
    catalogue = Catalogue(data, wrapper_prefix=WRAPPER_PREFIX)
    catalogue.create("demo", "cube(1);\n", ModelMeta(name="Demo"), thumbnail=PNG)

    assert [item.id for item in catalogue.list_media("demo")] == [LEGACY_ID]
    assert catalogue.record("demo").thumbnail_source == "model"


def test_without_a_database_a_media_write_is_refused(data: DataPaths) -> None:
    catalogue = Catalogue(data, wrapper_prefix=WRAPPER_PREFIX)
    catalogue.create("demo", "cube(1);\n", ModelMeta(name="Demo"), thumbnail=PNG)
    staged = _stage(catalogue, PNG, "image", "png")

    writes: list[Callable[[], object]] = [
        lambda: catalogue.add_media("demo", staged),
        lambda: catalogue.set_caption("demo", LEGACY_ID, "x"),
        lambda: catalogue.reorder("demo", [LEGACY_ID]),
        lambda: catalogue.remove_media("demo", LEGACY_ID),
    ]
    for write in writes:
        with pytest.raises(MediaUnavailableError):
            write()
    assert staged.path.exists()
    assert catalogue.thumbnail_path("demo").read_bytes() == PNG


def test_a_crashed_uploads_staging_is_swept_once_it_is_old(data: DataPaths) -> None:
    catalogue = Catalogue(data, wrapper_prefix=WRAPPER_PREFIX)
    catalogue.create("demo", "cube(1);\n", ModelMeta(name="Demo"))
    fresh = _stage(catalogue, PNG, "image", "png")
    stale = _stage(catalogue, PNG, "image", "png")
    old = time.time() - 2 * 3600
    os.utime(stale.path, (old, old))

    removed = catalogue.sweep_duplicate_staging()

    assert removed == [stale.path.name]
    assert fresh.path.exists()
