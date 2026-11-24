"""`Catalogue.template_row` and `template_stamp` (#7317, model records spec §5.1, plan
decisions 6 and 7): one template's files read into its derived row."""

from __future__ import annotations

import errno
import hashlib
import os
import uuid
from pathlib import Path
from typing import Any

import pytest

from scadbuddy.core.paths import DataPaths
from scadbuddy.library.catalogue import Catalogue, ModelMeta
from scadbuddy.library.history import ModelHistory
from scadbuddy.library.media import MEDIA_UPLOAD_PREFIX, MediaKind, StagedMedia
from scadbuddy.library.template_rows import (
    META_TEXT_MAX,
    TemplateRow,
    TemplateRows,
    template_stamp,
)
from scadbuddy.render.solids import WRAPPER_PREFIX
from tests.conftest import PgPool
from tests.support.media import MemoryMediaStore

pytestmark = pytest.mark.requires_git

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
WEBM = b"\x1a\x45\xdf\xa3\x01\x00" + b"\x00" * 10


@pytest.fixture
def paths(tmp_path: Path) -> DataPaths:
    data = DataPaths(root=tmp_path / "data")
    data.models.mkdir(parents=True)
    return data


@pytest.fixture
def catalogue(paths: DataPaths) -> Catalogue:
    history = ModelHistory(paths.models, wrapper_prefix=WRAPPER_PREFIX, timeout=10.0)
    history.ensure_repo()
    return Catalogue(paths, history, media_store=MemoryMediaStore(), wrapper_prefix=WRAPPER_PREFIX)


def _stage(catalogue: Catalogue, payload: bytes, kind: MediaKind, ext: str) -> StagedMedia:
    path = catalogue.paths.cache / f"{MEDIA_UPLOAD_PREFIX}{uuid.uuid4().hex}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return StagedMedia(path=path, kind=kind, extension=ext)


def _row(catalogue: Catalogue, template_id: str) -> TemplateRow:
    row = catalogue.template_row(
        template_id, version=catalogue.version(template_id), head="head-sha"
    )
    assert row is not None
    return row


def _stamp(catalogue: Catalogue, template_id: str) -> str:
    store = catalogue.media_store
    cover = store.cover(template_id) if template_id.startswith("builtin:") else None
    return template_stamp(catalogue.paths, template_id, store.items(template_id), cover)


def _builtin(paths: DataPaths) -> str:
    directory = paths.builtins / "keychain"
    (directory / "media").mkdir(parents=True)
    (directory / "model.scad").write_text("cube(1);\n", encoding="utf-8")
    (directory / "media" / "front.png").write_bytes(PNG)
    (directory / "model.json").write_text(
        '{"name": "Keychain", "media": [{"id": "front", "file": "front.png", "kind": "image"}]}',
        encoding="utf-8",
    )
    return "builtin:keychain"


def test_row_matches_the_record_of_a_plain_template(catalogue: Catalogue) -> None:
    catalogue.create("demo", "cube(1);\n", ModelMeta(name="Demo", tags=["a"]), readme="# Demo\n")
    row = _row(catalogue, "demo")
    record = catalogue.record("demo")
    assert row.template_id == "demo"
    assert row.origin == "mine"
    assert row.name == record.name == "Demo"
    assert row.updated_at == record.updated_at
    assert row.has_readme is record.has_readme is True
    assert row.inputs_version == record.inputs_version
    assert row.version == record.version
    assert row.version is not None
    assert row.refreshed_head == "head-sha"
    assert row.meta == catalogue.read_raw_meta("demo")
    assert row.meta_error is None
    assert row.meta_text is None
    assert row.files_stamp == _stamp(catalogue, "demo")


def test_row_files_match_the_media_views(catalogue: Catalogue, paths: DataPaths) -> None:
    catalogue.create("demo", "cube(1);\n", ModelMeta(name="Demo"))
    catalogue.add_media("demo", _stage(catalogue, PNG, "image", "png"))
    catalogue.add_media("demo", _stage(catalogue, WEBM, "video", "webm"))
    gone = catalogue.add_media("demo", _stage(catalogue, PNG + b"x", "image", "png")).media[2]
    (catalogue.media_dir("demo") / gone.file).unlink()

    views = catalogue.list_media("demo")
    row = _row(catalogue, "demo")

    assert [view.missing for view in views] == [False, False, True]
    assert row.files == {f"media/{view.file}": view.size for view in views if view.size is not None}
    assert f"media/{gone.file}" not in row.files


def test_row_cover_hash_is_the_thumbnail_hash_of_a_media_image(catalogue: Catalogue) -> None:
    catalogue.create("demo", "cube(1);\n", ModelMeta(name="Demo"))
    added = catalogue.add_media("demo", _stage(catalogue, PNG, "image", "png"))
    _assert_cover_is_the_thumbnail(catalogue, "demo", f"models/demo/media/{added.media[0].file}")


def test_row_cover_hash_is_the_thumbnail_hash_of_a_legacy_thumbnail(
    catalogue: Catalogue,
) -> None:
    catalogue.create("demo", "cube(1);\n", ModelMeta(name="Demo"), thumbnail=PNG)
    row = _assert_cover_is_the_thumbnail(catalogue, "demo", "models/demo/thumbnail.png")
    assert row.files == {"thumbnail.png": len(PNG)}


def test_row_cover_hash_is_the_thumbnail_hash_of_a_built_ins_chosen_cover(
    catalogue: Catalogue, paths: DataPaths
) -> None:
    builtin = _builtin(paths)
    added = catalogue.add_media(builtin, _stage(catalogue, PNG + b"mine", "image", "png"))
    chosen = added.media[1]
    catalogue.set_cover(builtin, chosen.id)
    row = _assert_cover_is_the_thumbnail(
        catalogue, builtin, f"builtin-media/keychain/{chosen.file}"
    )
    record = catalogue.record(builtin)
    assert row.origin == "builtin"
    assert row.name == record.name == "Keychain"
    assert row.updated_at == record.updated_at
    assert row.has_readme == record.has_readme
    assert row.inputs_version == record.inputs_version
    assert row.version == record.version
    assert row.files == {
        "media/front.png": len(PNG),
        f"builtin-media/{chosen.file}": len(PNG) + 4,
    }
    assert row.files_stamp == _stamp(catalogue, builtin)


def _assert_cover_is_the_thumbnail(
    catalogue: Catalogue, template_id: str, path: str
) -> TemplateRow:
    row = _row(catalogue, template_id)
    thumbnail = catalogue.thumbnail(template_id)
    assert thumbnail is not None
    assert row.cover == {
        "path": path,
        "content_type": thumbnail[1],
        "sha256": hashlib.sha256(thumbnail[0]).hexdigest(),
    }
    assert (catalogue.paths.root / path).read_bytes() == thumbnail[0]
    return row


def test_row_of_meta_that_is_not_json(catalogue: Catalogue, paths: DataPaths) -> None:
    catalogue.create("demo", "cube(1);\n", ModelMeta(name="Demo"))
    text = '{"name": "Demo", "libraries": ["BOSL2"' + " " * META_TEXT_MAX
    paths.model_meta("demo").write_text(text, encoding="utf-8")
    row = _row(catalogue, "demo")
    assert row.meta is None
    assert row.meta_error is not None
    assert row.meta_error.startswith("not JSON")
    assert row.meta_text == text[:META_TEXT_MAX]
    assert row.name == "demo"


def test_row_of_meta_that_does_not_parse_keeps_meta(catalogue: Catalogue, paths: DataPaths) -> None:
    catalogue.create("demo", "cube(1);\n", ModelMeta(name="Demo"))
    paths.model_meta("demo").write_text('{"name": "Demo", "tags": 5}', encoding="utf-8")
    row = _row(catalogue, "demo")
    assert row.meta == {"name": "Demo", "tags": 5}
    assert row.meta_error is not None
    assert "tags" in row.meta_error
    assert row.meta_text is None
    assert row.name == "demo"


def test_row_colour_params_are_the_color_annotations(catalogue: Catalogue) -> None:
    source = (
        'body = "#ff0000"; // color\n'
        "size = 10; // [1:20]\n"
        'text = "#00ff00"; // color\n'
        "cube(size);\n"
    )
    catalogue.create("demo", source, ModelMeta(name="Demo"))
    assert _row(catalogue, "demo").colour_params == ["body", "text"]


def test_no_model_scad_is_no_row(catalogue: Catalogue, paths: DataPaths) -> None:
    assert catalogue.template_row("absent", version=None, head=None) is None
    catalogue.create("demo", "cube(1);\n", ModelMeta(name="Demo"))
    paths.model_source("demo").unlink()
    assert catalogue.template_row("demo", version=None, head=None) is None


def test_a_template_path_that_is_a_file_is_no_row(catalogue: Catalogue, paths: DataPaths) -> None:
    # Review M2: NotADirectoryError answers as a missing file does.
    paths.model_dir("stray").write_text("not a folder", encoding="utf-8")
    assert catalogue.template_row("stray", version=None, head=None) is None
    assert template_stamp(paths, "stray", [], None) == template_stamp(paths, "absent", [], None)


def test_stamp_is_stable_when_nothing_changes(catalogue: Catalogue) -> None:
    catalogue.create("demo", "cube(1);\n", ModelMeta(name="Demo"), readme="x")
    catalogue.add_media("demo", _stage(catalogue, PNG, "image", "png"))
    assert _stamp(catalogue, "demo") == _stamp(catalogue, "demo")
    assert _row(catalogue, "demo").files_stamp == _stamp(catalogue, "demo")


def test_stamp_changes_when_model_json_is_touched(catalogue: Catalogue, paths: DataPaths) -> None:
    catalogue.create("demo", "cube(1);\n", ModelMeta(name="Demo"))
    before = _stamp(catalogue, "demo")
    meta = paths.model_meta("demo")
    info = meta.stat()
    os.utime(meta, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000))
    assert _stamp(catalogue, "demo") != before


def test_stamp_changes_when_a_media_file_is_added(catalogue: Catalogue) -> None:
    catalogue.create("demo", "cube(1);\n", ModelMeta(name="Demo"))
    catalogue.add_media("demo", _stage(catalogue, PNG, "image", "png"))
    before = _stamp(catalogue, "demo")
    (catalogue.media_dir("demo") / "stray.png").write_bytes(PNG)
    assert _stamp(catalogue, "demo") != before


def test_stamp_changes_when_media_rows_reorder(catalogue: Catalogue) -> None:
    catalogue.create("demo", "cube(1);\n", ModelMeta(name="Demo"))
    one = catalogue.add_media("demo", _stage(catalogue, PNG, "image", "png")).media[0]
    two = catalogue.add_media("demo", _stage(catalogue, PNG, "image", "png")).media[1]
    before = _stamp(catalogue, "demo")
    catalogue.reorder("demo", [two.id, one.id])
    assert _stamp(catalogue, "demo") != before


def test_stamp_raises_on_an_os_error_other_than_not_found(
    catalogue: Catalogue, paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalogue.create("demo", "cube(1);\n", ModelMeta(name="Demo"))
    stale = os.fspath(paths.model_meta("demo"))
    real_stat = os.stat

    def stat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        if os.fspath(path) == stale:
            raise OSError(errno.ESTALE, "Stale file handle")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", stat)
    with pytest.raises(OSError) as raised:
        _stamp(catalogue, "demo")
    assert raised.value.errno == errno.ESTALE


def test_stamp_raises_when_a_folder_listing_is_stale(
    catalogue: Catalogue, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Review M3: ESTALE on the listing of media/, not only on a file's stat.
    catalogue.create("demo", "cube(1);\n", ModelMeta(name="Demo"))
    catalogue.add_media("demo", _stage(catalogue, PNG, "image", "png"))
    stale = os.fspath(catalogue.media_dir("demo"))
    real_listdir = os.listdir

    def listdir(path: Any = ".") -> list[str]:
        if os.fspath(path) == stale:
            raise OSError(errno.ESTALE, "Stale file handle")
        return real_listdir(path)

    monkeypatch.setattr(os, "listdir", listdir)
    with pytest.raises(OSError) as raised:
        _stamp(catalogue, "demo")
    assert raised.value.errno == errno.ESTALE


def _meta_reads(
    monkeypatch: pytest.MonkeyPatch, meta: Path, answer: Exception | None = None
) -> list[str]:
    """Count the reads of ``meta``, and raise ``answer`` from each when given."""
    reads: list[str] = []
    for name in ("read_bytes", "read_text"):
        real = getattr(Path, name)

        def read(self: Path, *args: Any, _real: Any = real, _name: str = name, **kw: Any) -> Any:
            if self == meta:
                reads.append(_name)
                if answer is not None:
                    raise answer
            return _real(self, *args, **kw)

        monkeypatch.setattr(Path, name, read)
    return reads


def test_row_reads_meta_that_is_not_json_once(
    catalogue: Catalogue, paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Review M1: a second read could see another file, or none.
    catalogue.create("demo", "cube(1);\n", ModelMeta(name="Demo"))
    paths.model_meta("demo").write_text("{not json", encoding="utf-8")
    reads = _meta_reads(monkeypatch, paths.model_meta("demo"))
    row = _row(catalogue, "demo")
    assert row.meta_text == "{not json"
    assert len(reads) == 1


def test_row_of_meta_deleted_while_read_is_a_missing_meta(
    catalogue: Catalogue, paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalogue.create("demo", "cube(1);\n", ModelMeta(name="Demo"))
    _meta_reads(monkeypatch, paths.model_meta("demo"), FileNotFoundError("gone"))
    row = _row(catalogue, "demo")
    assert row.meta == {}
    assert row.meta_error is None
    assert row.meta_text is None
    assert row.name == "demo"


@pytest.mark.requires_postgres
@pytest.mark.parametrize(
    "text",
    [
        '{"name": "D", "x": NaN}',
        '{"name": "D", "x": Infinity}',
        '{"name": "D", "tags": ["\\ud800"]}',
        '{"name": "D", "x": "a\\u0000b"}',
        '{"name": "D", "\\u0000k": 1}',
        '{"name": "D\\u0000"}',
    ],
    ids=["nan", "infinity", "lone-surrogate", "nul-in-value", "nul-in-key", "nul-in-name"],
)
def test_row_of_meta_postgres_cannot_store_is_written_without_meta(
    catalogue: Catalogue, paths: DataPaths, pg_pool: PgPool, text: str
) -> None:
    # Review I1 (#7317): Python reads each of these, and jsonb or a text column refuses it.
    catalogue.create("demo", "cube(1);\n", ModelMeta(name="Demo"))
    paths.model_meta("demo").write_text(text, encoding="utf-8")
    row = _row(catalogue, "demo")
    assert row.meta is None
    assert row.meta_error is not None
    assert row.meta_error.startswith("not storable")
    assert row.meta_text == text
    assert row.name == "demo"
    rows = TemplateRows(pg_pool)
    rows.put([row], [])
    assert rows.get("demo") == row
