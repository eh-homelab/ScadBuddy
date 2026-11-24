"""Every catalogue write refreshes its template rows (#7317, model records spec §5.2,
plan "Every place that writes `model.json` or moves the models HEAD").

After each writer, the row equals what `Catalogue.template_row` reads from the files at
that moment: the rows written in the HEAD transaction, by the no-commit path, by
`_overlay_change` or, without git, by `notify_change`.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest

from scadbuddy.core.gitrepo import GitError, GitTimeoutError
from scadbuddy.core.paths import DataPaths
from scadbuddy.library.catalogue import Catalogue, InvalidModelMetaError, ModelMeta, ModelPatch
from scadbuddy.library.history import ModelHistory
from scadbuddy.library.libraries import ModelLibrary
from scadbuddy.library.media import MEDIA_UPLOAD_PREFIX, MediaKind, StagedMedia
from scadbuddy.library.media_store import PostgresMediaStore
from scadbuddy.library.repo_lock import HEAD_CHANNEL, PgRepoCoordinator, decode_change
from scadbuddy.library.template_refresh import TemplateRefresh
from scadbuddy.library.template_rows import TemplateRow, TemplateRows
from scadbuddy.render.solids import WRAPPER_PREFIX
from tests.conftest import PgPool, open_pg_pool

pytestmark = [pytest.mark.requires_git, pytest.mark.requires_postgres]

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
WEBM = b"\x1a\x45\xdf\xa3\x01\x00" + b"\x00" * 10
SOURCE = 'colour = "red"; // color\ncube(1);\n'
COMMIT = "0123456789abcdef0123456789abcdef01234567"


@dataclass
class World:
    catalogue: Catalogue
    history: ModelHistory | None
    rows: TemplateRows
    image: Path
    failures: list[str]


@pytest.fixture
def pool(pg_conninfo: str) -> Iterator[PgPool]:
    opened = open_pg_pool(pg_conninfo)
    try:
        yield opened
    finally:
        opened.close()


def _world(tmp_path: Path, pool: PgPool, conninfo: str | None) -> World:
    paths = DataPaths(tmp_path / "data")
    paths.ensure()
    history: ModelHistory | None = None
    if conninfo is not None:
        history = ModelHistory(
            paths.models,
            wrapper_prefix=WRAPPER_PREFIX,
            timeout=10.0,
            coordinator=PgRepoCoordinator(conninfo),
        )
        history.ensure_repo()
    catalogue = Catalogue(
        paths, history, media_store=PostgresMediaStore(pool), wrapper_prefix=WRAPPER_PREFIX
    )
    rows = TemplateRows(pool)
    failures: list[str] = []
    refresh = TemplateRefresh(catalogue, rows, on_failure=failures.append)
    catalogue.refresh = refresh
    if history is not None:
        history.template_rows = refresh
    image = tmp_path / "image"
    _bundle(image, "keychain", "cube(10);\n")
    return World(catalogue, history, rows, image, failures)


@pytest.fixture
def world(tmp_path: Path, pool: PgPool, pg_conninfo: str) -> World:
    found = _world(tmp_path, pool, pg_conninfo)
    found.catalogue.sync_builtins(found.image)
    found.catalogue.create("demo", SOURCE, ModelMeta(name="Demo"))
    return found


def _bundle(root: Path, slug: str, source: str) -> Path:
    directory = root / slug
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "model.scad").write_text(source, encoding="utf-8")
    (directory / "model.json").write_text(json.dumps({"name": slug}), encoding="utf-8")
    return directory


def _stage(catalogue: Catalogue, payload: bytes, kind: MediaKind, ext: str) -> StagedMedia:
    path = catalogue.paths.cache / f"{MEDIA_UPLOAD_PREFIX}{uuid.uuid4().hex}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return StagedMedia(path=path, kind=kind, extension=ext)


def _image(catalogue: Catalogue, slug: str) -> str:
    catalogue.add_media(slug, _stage(catalogue, PNG, "image", "png"))
    return catalogue.media_store.items(slug)[-1].id


def _stale(template_id: str) -> TemplateRow:
    return TemplateRow(
        template_id=template_id,
        origin="builtin" if template_id.startswith("builtin:") else "mine",
        meta={},
        meta_error=None,
        meta_text=None,
        name="stale",
        version=None,
        updated_at=datetime(2000, 1, 1, tzinfo=UTC),
        has_readme=False,
        inputs_version=0,
        colour_params=None,
        files={},
        cover=None,
        files_stamp="stale",
        refreshed_head=None,
    )


def _strip(row: TemplateRow | None) -> TemplateRow | None:
    return None if row is None else dataclasses.replace(row, refreshed_head=None)


def _expected(world: World, model_id: str) -> TemplateRow | None:
    head = None if world.history is None else world.history.head()
    version = world.catalogue.version(model_id)
    return world.catalogue.template_row(model_id, version=version, head=head)


# ── the writers ──────────────────────────────────────────────────────────────
# Each answers the template to check. `setup` runs first, then the row is made stale,
# then `act` runs: the row is right only when `act` refreshed it.


@dataclass(frozen=True)
class Writer:
    model_id: str
    act: Callable[[World], object]
    setup: Callable[[World], object] = lambda _: None
    gone: bool = False


def _seeded(world: World) -> None:
    catalogue, history = world.catalogue, world.history
    assert history is not None
    directory = catalogue.paths.model_dir("name-keychain")
    directory.mkdir(parents=True)
    (directory / "model.scad").write_text("cube(3);\n", encoding="utf-8")
    (directory / "model.json").write_text('{"name": "name-keychain"}', encoding="utf-8")
    history.commit("Seed name-keychain from the image", "name-keychain")
    _bundle(world.image, "name-keychain", "cube(3);\n")
    catalogue.sync_builtins(world.image)


def _restore(world: World) -> None:
    assert world.history is not None
    first = world.catalogue.version("demo")
    assert first is not None
    world.catalogue.write_source("demo", "cube(4);\n")
    world.history.restore("demo", first)


def _upstream_update(world: World) -> None:
    world.catalogue.duplicate("demo", "copy", "Copy")
    world.catalogue.write_source("demo", SOURCE.replace("cube(1)", "cube(5)"))


def _upstream_gone(world: World) -> None:
    world.catalogue.duplicate("demo", "copy", "Copy")
    world.catalogue.delete("demo")


def _two_images(world: World) -> None:
    _image(world.catalogue, "demo")
    _image(world.catalogue, "demo")


def _two_builtin_images(world: World) -> None:
    _image(world.catalogue, "builtin:keychain")
    _image(world.catalogue, "builtin:keychain")


def _merge_resolve(world: World) -> None:
    """The saved resolution of a conflicted upstream merge (`write_source` with
    ``merge_base``): the upstream's revision becomes the copy's base."""
    base = world.catalogue.version("demo")
    assert base is not None
    world.catalogue.write_source("copy", "cube(6);\n", merge_base=base)


def _ids(world: World, slug: str) -> list[str]:
    return [item.id for item in world.catalogue.media_store.items(slug)]


LIBRARY = ModelLibrary(
    name="BOSL2", url="https://github.com/BelfrySCAD/BOSL2.git", ref="v1", commit=COMMIT
)

WRITERS: dict[str, Writer] = {
    "update": Writer("demo", lambda w: w.catalogue.update("demo", ModelPatch(description="nicer"))),
    "pin_library": Writer("demo", lambda w: w.catalogue.pin_library("demo", LIBRARY)),
    "unpin_library": Writer(
        "demo",
        lambda w: w.catalogue.unpin_library("demo", "BOSL2"),
        lambda w: w.catalogue.pin_library("demo", LIBRARY),
    ),
    "write_file": Writer(
        "demo", lambda w: w.catalogue.write_file("demo", "extra.scad", "cube(2);")
    ),
    "update_files": Writer(
        "demo",
        lambda w: w.catalogue.update_files(
            "demo", lambda _: {"model.json": b'{"name": "Files"}'}, message="Edit demo files"
        ),
    ),
    "write_source": Writer("demo", lambda w: w.catalogue.write_source("demo", "cube(2);\n")),
    "write_readme": Writer("demo", lambda w: w.catalogue.write_readme("demo", "# Demo\n")),
    "delete_readme": Writer(
        "demo",
        lambda w: w.catalogue.delete_readme("demo"),
        lambda w: w.catalogue.write_readme("demo", "# Demo\n"),
    ),
    "write_thumbnail": Writer("demo", lambda w: w.catalogue.write_thumbnail("demo", PNG)),
    "delete_thumbnail": Writer(
        "demo",
        lambda w: w.catalogue.delete_thumbnail("demo"),
        lambda w: w.catalogue.write_thumbnail("demo", PNG),
    ),
    "add_media_image": Writer("demo", lambda w: _image(w.catalogue, "demo")),
    "add_media_video": Writer(
        "demo",
        lambda w: w.catalogue.add_media("demo", _stage(w.catalogue, WEBM, "video", "webm")),
    ),
    "set_caption": Writer(
        "demo",
        lambda w: w.catalogue.set_caption("demo", _ids(w, "demo")[0], "A caption"),
        lambda w: _image(w.catalogue, "demo"),
    ),
    "reorder": Writer(
        "demo",
        lambda w: w.catalogue.reorder("demo", list(reversed(_ids(w, "demo")))),
        _two_images,
    ),
    "remove_media": Writer(
        "demo",
        lambda w: w.catalogue.remove_media("demo", _ids(w, "demo")[0]),
        lambda w: _image(w.catalogue, "demo"),
    ),
    "set_cover": Writer(
        "demo", lambda w: w.catalogue.set_cover("demo", _ids(w, "demo")[1]), _two_images
    ),
    "builtin_add_media": Writer(
        "builtin:keychain", lambda w: _image(w.catalogue, "builtin:keychain")
    ),
    "builtin_set_cover": Writer(
        "builtin:keychain",
        lambda w: w.catalogue.set_cover("builtin:keychain", _ids(w, "builtin:keychain")[0]),
        lambda w: _image(w.catalogue, "builtin:keychain"),
    ),
    "builtin_set_caption": Writer(
        "builtin:keychain",
        lambda w: w.catalogue.set_caption(
            "builtin:keychain", _ids(w, "builtin:keychain")[0], "A caption"
        ),
        lambda w: _image(w.catalogue, "builtin:keychain"),
    ),
    "builtin_reorder": Writer(
        "builtin:keychain",
        lambda w: w.catalogue.reorder(
            "builtin:keychain", list(reversed(_ids(w, "builtin:keychain")))
        ),
        _two_builtin_images,
    ),
    "builtin_remove_media": Writer(
        "builtin:keychain",
        lambda w: w.catalogue.remove_media("builtin:keychain", _ids(w, "builtin:keychain")[0]),
        lambda w: _image(w.catalogue, "builtin:keychain"),
    ),
    "merge_resolve": Writer("copy", _merge_resolve, _upstream_update),
    "merge_upstream": Writer(
        "copy", lambda w: w.catalogue.merge_upstream("copy"), _upstream_update
    ),
    "dismiss_upstream": Writer(
        "copy", lambda w: w.catalogue.dismiss_upstream("copy"), _upstream_update
    ),
    "detach_upstream": Writer(
        "copy", lambda w: w.catalogue.detach_upstream("copy"), _upstream_gone
    ),
    "duplicate": Writer("copy", lambda w: w.catalogue.duplicate("demo", "copy", "Copy")),
    "create": Writer(
        "fresh", lambda w: w.catalogue.create("fresh", SOURCE, ModelMeta(name="Fresh"))
    ),
    "delete": Writer("demo", lambda w: w.catalogue.delete("demo"), gone=True),
    "sync_builtins": Writer(
        "builtin:keychain",
        lambda w: w.catalogue.sync_builtins(w.image),
        lambda w: _bundle(w.image, "keychain", "cube(11);\n"),
    ),
    "link_seeded": Writer("name-keychain", lambda w: w.catalogue.link_seeded(), _seeded),
    "history_restore": Writer("demo", _restore),
}


@pytest.mark.parametrize("name", list(WRITERS))
def test_every_writer_leaves_the_row_equal_to_the_walk(world: World, name: str) -> None:
    writer = WRITERS[name]
    writer.setup(world)
    world.rows.put([_stale(writer.model_id)], [])

    writer.act(world)

    found = world.rows.get(writer.model_id)
    if writer.gone:
        assert found is None
    else:
        assert found is not None
        assert _strip(found) == _strip(_expected(world, writer.model_id))
    assert world.failures == []


def test_without_git_create_update_delete_refresh_through_notify_change(
    tmp_path: Path, pool: PgPool
) -> None:
    world = _world(tmp_path, pool, None)
    catalogue = world.catalogue

    catalogue.create("demo", SOURCE, ModelMeta(name="Demo"))
    created = world.rows.get("demo")
    assert created is not None and created.name == "Demo" and created.version is None
    assert _strip(created) == _strip(_expected(world, "demo"))

    catalogue.update("demo", ModelPatch(name="Renamed"))
    updated = world.rows.get("demo")
    assert updated is not None and updated.name == "Renamed"
    assert _strip(updated) == _strip(_expected(world, "demo"))

    catalogue.delete("demo")
    assert world.rows.get("demo") is None
    assert world.failures == []


def test_a_failed_refresh_is_logged_counted_and_the_write_stands(
    world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    item = _image(world.catalogue, "demo")

    def refuse(*_: object, **__: object) -> None:
        raise psycopg.OperationalError("the database is gone")

    monkeypatch.setattr(world.rows, "put", refuse)

    record = world.catalogue.set_caption("demo", item, "Kept")

    assert [view.caption for view in record.media] == ["Kept"]
    assert world.failures == ["no_commit"]
    assert any("demo" in str(getattr(entry, "templates", "")) for entry in caplog.records), (
        "the log names the template"
    )


def test_a_read_that_fails_for_a_commit_is_counted_and_the_commit_stands(
    world: World, monkeypatch: pytest.MonkeyPatch, pg_conninfo: str
) -> None:
    assert world.history is not None

    def broken(*_: object, **__: object) -> None:
        raise RuntimeError("the read broke")

    monkeypatch.setattr(world.catalogue, "template_row", broken)

    world.catalogue.update("demo", ModelPatch(description="kept"))

    assert world.catalogue.record("demo").description == "kept"
    assert world.failures == ["commit"]
    head = world.history.head()
    assert world.history.log("demo")[0].commit == head
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        found = conn.execute("SELECT head FROM models_repo_head WHERE id").fetchone()
    assert found is not None and found[0] == head


def test_a_write_that_fails_for_a_commit_is_counted_and_the_head_stands(
    world: World, monkeypatch: pytest.MonkeyPatch, pg_conninfo: str
) -> None:
    """Review B-I1 (#7317): the rows' write fails in its savepoint; the HEAD is recorded."""
    assert world.history is not None
    before = world.rows.get("demo")

    def refuse(*_: object, **__: object) -> None:
        raise psycopg.errors.NumericValueOutOfRange("integer out of range")

    monkeypatch.setattr("scadbuddy.library.template_refresh.write_rows", refuse)

    world.catalogue.update("demo", ModelPatch(description="kept"))

    assert world.catalogue.record("demo").description == "kept"
    assert world.failures == ["commit"]
    assert world.rows.get("demo") == before
    head = world.history.head()
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        found = conn.execute("SELECT head FROM models_repo_head WHERE id").fetchone()
    assert found is not None and found[0] == head


def test_a_create_whose_commit_fails_leaves_no_row(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review B-I1 of Task 4 (#7317): the no-commit refresh reads the folder before the
    create's undo removes it; the row must go with the folder."""
    history = world.history
    assert history is not None

    def broken(*_: object, **__: object) -> None:
        raise GitError("git commit failed")

    monkeypatch.setattr(history, "_commit_action", broken)

    with pytest.raises(GitError):
        world.catalogue.create("ghost", SOURCE, ModelMeta(name="Ghost"))

    assert not world.catalogue.exists("ghost")
    assert world.rows.get("ghost") is None


def test_a_failed_refresh_after_an_undo_is_counted(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 6 amendment 1 (#7317): when the undo's refresh cannot take the write lock, the
    failure is counted under ``no_commit``, as a failed refresh_locked is, and the create
    still raises its own error."""
    history = world.history
    assert history is not None

    def broken(*_: object, **__: object) -> None:
        raise GitError("git commit failed")

    def no_lock() -> None:
        raise OSError("the lock file cannot be opened")

    monkeypatch.setattr(history, "_commit_action", broken)
    monkeypatch.setattr(history, "write_lock", no_lock)

    with pytest.raises(GitError):
        world.catalogue.create("ghost", SOURCE, ModelMeta(name="Ghost"))

    assert world.failures == ["no_commit"]


def test_the_built_in_media_refresh_runs_under_the_write_lock(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    history = world.history
    assert history is not None
    refresh = world.catalogue.refresh
    assert refresh is not None
    held: list[tuple[list[str], str, bool]] = []
    real = refresh.refresh_locked

    def noted(models: list[str], *, path: str) -> None:
        held.append((list(models), path, history._lock.locked() and history._hold is not None))
        real(models, path=path)  # type: ignore[arg-type]

    monkeypatch.setattr(refresh, "refresh_locked", noted)

    _image(world.catalogue, "builtin:keychain")

    assert held == [(["builtin:keychain"], "no_commit", True)]


def test_a_commit_that_cannot_take_the_write_lock_is_counted(
    world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Final review M1 (#7317): `_commit` writes the files first. When the lock wait then
    fails, nothing refreshes the row, so the miss is logged with the template and counted
    under ``no_commit``: the next copy's repair is not read as a missed writer."""
    history = world.history
    assert history is not None
    world.failures.clear()

    def no_lock(*_: object, **__: object) -> None:
        raise GitTimeoutError("waited 10s for the in-process write lock")

    monkeypatch.setattr(history, "_exclusive", no_lock)

    record = world.catalogue.write_readme("demo", "# Demo\n")

    assert record.has_readme
    assert world.failures == ["no_commit"]
    assert any("demo" in str(getattr(entry, "templates", "")) for entry in caplog.records), (
        "the log names the template"
    )


def test_a_failed_commit_under_the_lock_is_not_counted_twice(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Final review M1 (#7317): a commit that fails under the lock has refreshed the row
    already (`_refresh_uncommitted`); `_commit` counts nothing more."""
    history = world.history
    assert history is not None
    world.failures.clear()

    def broken(*_: object, **__: object) -> None:
        raise GitError("git commit failed")

    monkeypatch.setattr(history, "_commit_locked", broken)

    world.catalogue.write_readme("demo", "# Demo\n")

    assert world.failures == []
    assert _strip(world.rows.get("demo")) == _strip(_expected(world, "demo"))


def test_a_refresh_keeps_the_stored_version_when_git_does_not_answer(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Final review M2 (#7317), as the copy's Task 5 B-M2 fix: a no-commit refresh whose
    last-commit read fails keeps the row's stored version, never None, and counts the
    failure under ``no_commit``."""
    history = world.history
    assert history is not None
    item = _image(world.catalogue, "demo")
    before = world.rows.get("demo")
    assert before is not None and before.version is not None
    world.failures.clear()

    def no_answer(*_: object, **__: object) -> str | None:
        raise GitError("git log failed")

    monkeypatch.setattr(history, "last_commit_checked", no_answer)
    monkeypatch.setattr(history, "last_commit", lambda *_: None)

    world.catalogue.set_caption("demo", item, "Kept")

    after = world.rows.get("demo")
    assert after is not None
    assert after.version == before.version
    assert after.files_stamp != before.files_stamp
    assert world.failures == ["no_commit"]


def _save_meta(text: bytes) -> Callable[[World], object]:
    def save(world: World) -> object:
        # A model.json that is not JSON commits, then its record does not read.
        with contextlib.suppress(InvalidModelMetaError):
            return world.catalogue.update_files(
                "demo", lambda _: {"model.json": text}, message="Edit model.json"
            )
        return None

    return save


def _save_description(world: World) -> object:
    return world.catalogue.update("demo", ModelPatch(description="a\x00b"))


@pytest.mark.parametrize(
    ("save", "error"),
    [
        (_save_meta(b'{"name": "D\\u0000"}'), "not storable"),
        (_save_meta(b'{"name": "D", "description": "a\\u0000b"}'), "not storable"),
        (_save_meta(b'{"name": "D", "params": {"size\\u0000": "\\u0000"}}'), "not storable"),
        (_save_meta(b'{"name": "D\x00"}'), "not JSON"),
        (_save_meta(b"not json \x00 at all"), "not JSON"),
        (_save_description, "not storable"),
    ],
    ids=[
        "escape-in-name",
        "escape-in-description",
        "escape-in-params",
        "nul-byte-in-json",
        "nul-byte-not-json",
        "description-patch",
    ],
)
def test_a_saved_nul_records_the_head_sends_the_notify_and_writes_the_row(
    world: World, pg_conninfo: str, save: Callable[[World], object], error: str
) -> None:
    """Review of #7484 (Blocking): a NUL byte, which a text column refuses, or a
    ``\\u0000`` escape, which jsonb refuses, is cleaned when the row is built. The save
    records its HEAD, sends its NOTIFY and writes the row in that transaction."""
    history = world.history
    assert history is not None
    with psycopg.connect(pg_conninfo, autocommit=True) as listener:
        listener.execute(f"LISTEN {HEAD_CHANNEL}")
        save(world)
        heard = [
            decode_change(notice.payload)
            for notice in listener.notifies(timeout=0.5, stop_after=None)
        ]
        found = listener.execute("SELECT head FROM models_repo_head WHERE id").fetchone()
    head = history.head()
    assert world.catalogue.version("demo") == head
    assert found is not None and found[0] == head
    assert [change.commit for change in heard] == [head]
    assert heard[0].models == ["demo"]
    assert world.failures == []
    row = world.rows.get("demo")
    assert row is not None
    assert row.version == head and row.refreshed_head == head
    assert row.meta is None
    assert row.meta_error is not None and row.meta_error.startswith(error)
    assert row.meta_text is not None and "\x00" not in row.meta_text
    assert row == _expected(world, "demo")


def test_a_row_postgres_refuses_still_records_the_head_and_its_notify(
    world: World, pg_conninfo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #7484 (Blocking), the second guard: with the build's cleaning turned
    off, Postgres itself refuses the row's ``\\u0000``. The refusal rolls back the rows'
    savepoint only: the HEAD is recorded, its NOTIFY sent, the failure counted, and the
    row keeps what it had until the copy repairs it."""
    history = world.history
    assert history is not None
    before = world.rows.get("demo")
    monkeypatch.setattr("scadbuddy.library.catalogue._unstorable", lambda _: None)
    with psycopg.connect(pg_conninfo, autocommit=True) as listener:
        listener.execute(f"LISTEN {HEAD_CHANNEL}")
        _save_meta(b'{"name": "Demo", "description": "a\\u0000b"}')(world)
        heard = [
            decode_change(notice.payload).commit
            for notice in listener.notifies(timeout=0.5, stop_after=None)
        ]
        found = listener.execute("SELECT head FROM models_repo_head WHERE id").fetchone()
    head = history.head()
    assert world.catalogue.version("demo") == head
    assert found is not None and found[0] == head
    assert heard == [head]
    assert world.failures == ["commit"]
    assert world.rows.get("demo") == before
