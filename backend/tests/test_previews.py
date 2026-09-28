"""The default-render preview store (#179 follow-up; in Postgres since #454) and its
place in the catalogue."""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import NoReturn
from unittest import mock

import psycopg
import pytest
import trimesh

from scadbuddy.core.config import Config
from scadbuddy.core.paths import DataPaths
from scadbuddy.library.assets import AssetStore
from scadbuddy.library.catalogue import Catalogue
from scadbuddy.library.libraries import CheckoutGate
from scadbuddy.library.previews import (
    PREVIEW_ID_LENGTH,
    PreviewStore,
    new_work_dir,
    source_key,
    sweep_work_dirs,
)
from scadbuddy.render import previews as previews_module
from scadbuddy.render.jobs import ModelSource
from scadbuddy.render.pg_store import PostgresJobStore
from scadbuddy.render.previews import render_preview
from scadbuddy.render.runner import OpenSCADError
from scadbuddy.render.schema import CustomizerSchema, Parameter
from tests.conftest import write_openscad_3mf

SLUG = "widget"
CONFIG = Config(data_dir=Path("/unused"))


@pytest.fixture
def paths(tmp_path: Path) -> DataPaths:
    data = DataPaths(tmp_path / "data")
    data.ensure()
    data.model_dir(SLUG).mkdir(parents=True)
    data.model_source(SLUG).write_text("cube(10);\n", encoding="utf-8")
    return data


def test_the_source_key_follows_the_source_and_its_libraries_only(paths: DataPaths) -> None:
    key = source_key(paths, SLUG)

    (paths.model_dir(SLUG) / "README.md").write_text("# Widget\n", encoding="utf-8")
    paths.model_meta(SLUG).write_text(json.dumps({"name": "Widget"}), encoding="utf-8")
    assert source_key(paths, SLUG) == key

    paths.model_meta(SLUG).write_text(json.dumps({"libraries": ["BOSL2"]}), encoding="utf-8")
    with_library = source_key(paths, SLUG)
    assert with_library != key

    paths.model_source(SLUG).write_text("cube(12);\n", encoding="utf-8")
    assert source_key(paths, SLUG) not in (key, with_library)
    assert source_key(paths, "gone") is None


@pytest.fixture
def store(pg_conninfo: str, paths: DataPaths) -> Iterator[PreviewStore]:
    database = PostgresJobStore(pg_conninfo, paths, pool_size=3)
    database.open()
    try:
        yield PreviewStore(database.connection)
    finally:
        database.close()


@pytest.mark.requires_postgres
def test_a_preview_is_served_only_once_rendered_and_a_failure_serves_nothing(
    store: PreviewStore,
) -> None:
    assert (store.image(SLUG), store.preview_id(SLUG)) == (None, None)

    store.write(SLUG, "a" * 64, b"png")
    assert store.image(SLUG) == b"png"
    assert store.preview_id(SLUG) == "a" * PREVIEW_ID_LENGTH

    store.record_failure(SLUG, "b" * 64, "openscad exited with 1")
    record = store.record(SLUG)
    assert record is not None
    assert (record.key, record.ok, record.error) == ("b" * 64, False, "openscad exited with 1")
    assert (store.image(SLUG), store.preview_id(SLUG)) == (None, None)

    store.drop(SLUG)
    assert store.record(SLUG) is None


@pytest.mark.requires_postgres
def test_a_builtin_id_is_a_key_like_any_other(store: PreviewStore) -> None:
    store.write("builtin:widget", "a" * 64, b"builtin")
    store.write(SLUG, "b" * 64, b"mine")

    assert store.image("builtin:widget") == b"builtin"
    assert store.slugs() == ["builtin:widget", SLUG]


@pytest.mark.requires_postgres
def test_the_orphan_sweep_takes_a_gone_models_preview_only(
    store: PreviewStore, paths: DataPaths
) -> None:
    store.write("gone", "a" * 64, b"png")
    store.write(SLUG, "a" * 64, b"png")

    removed = Catalogue(paths, previews=store).sweep_orphan_previews()

    assert removed == ["gone"]
    assert store.record("gone") is None
    assert store.image(SLUG) == b"png"
    # The file sweep has nothing of the previews' to look at.
    assert Catalogue(paths, previews=store).sweep_orphans() == []


def test_the_orphan_sweep_logs_and_skips_when_the_database_cannot_list(
    paths: DataPaths, caplog: pytest.LogCaptureFixture
) -> None:
    """As `sweep_orphans` skips a root it cannot list: a database error costs this
    sweep, never the boot."""

    def unreachable() -> NoReturn:
        raise psycopg.OperationalError("the database went away")

    with caplog.at_level("ERROR"):
        removed = Catalogue(paths, previews=PreviewStore(unreachable)).sweep_orphan_previews()

    assert removed == []
    assert "could not list the previews to sweep" in caplog.text


def test_the_boot_sweep_takes_a_crashed_renders_scratch_but_not_a_live_one(
    paths: DataPaths,
) -> None:
    crashed = new_work_dir(paths)
    (crashed / "parts").mkdir(parents=True)
    (crashed / "parts" / "raw.3mf").write_bytes(b"raw")
    os.utime(crashed, (time.time() - 3600, time.time() - 3600))
    live = new_work_dir(paths)
    live.mkdir()

    removed = sweep_work_dirs(paths, max_age=600)

    assert removed == [crashed.name]
    assert not crashed.exists()
    assert live.exists()


@pytest.mark.requires_postgres
def test_the_catalogue_ranks_its_own_image_over_the_preview(
    store: PreviewStore, paths: DataPaths
) -> None:
    catalogue = Catalogue(paths, previews=store)
    store.write(SLUG, "a" * 64, b"preview")

    origin = catalogue.thumbnail_source(SLUG)
    assert (origin.source, origin.preview_id) == ("preview", "a" * PREVIEW_ID_LENGTH)
    assert catalogue.thumbnail(SLUG) == (b"preview", "image/png")

    catalogue.write_thumbnail(SLUG, b"own")
    assert catalogue.thumbnail_source(SLUG).source == "model"
    assert catalogue.thumbnail(SLUG) == (b"own", "image/png")
    assert store.image(SLUG) is None


@pytest.mark.requires_postgres
def test_a_catalogue_not_serving_previews_shows_none(store: PreviewStore, paths: DataPaths) -> None:
    store.write(SLUG, "a" * 64, b"preview")
    catalogue = Catalogue(paths, previews=store, serve_previews=False)

    assert catalogue.thumbnail_source(SLUG).source is None
    assert catalogue.thumbnail(SLUG) is None


@pytest.mark.requires_postgres
def test_a_write_no_longer_wanted_writes_nothing(store: PreviewStore) -> None:
    assert store.write(SLUG, "a" * 64, b"png", wanted=lambda: False) is False
    assert store.record_failure(SLUG, "a" * 64, "boom", wanted=lambda: False) is False
    assert store.record(SLUG) is None
    assert store.image(SLUG) is None


@pytest.mark.requires_postgres
def test_a_preview_or_a_failure_is_current_for_its_own_source_only(store: PreviewStore) -> None:
    store.write(SLUG, "a" * 64, b"png")
    assert store.current(SLUG, "a" * 64)
    assert not store.current(SLUG, "b" * 64)

    # A failure has no image; it stays current, so it is not retried.
    store.record_failure(SLUG, "b" * 64, "boom")
    assert store.current(SLUG, "b" * 64)
    assert not store.current(SLUG, "c" * 64)
    assert not store.current("gone", "a" * 64)


@pytest.mark.requires_postgres
def test_a_drop_cannot_land_between_the_check_and_the_write(store: PreviewStore) -> None:
    """The render's liveness check and its write are one step against a drop: a drop
    that arrives mid-write waits, then removes the preview."""
    writer, _, release = _held_write(store)
    dropper = threading.Thread(target=lambda: store.drop(SLUG))
    dropper.start()
    dropper.join(0.2)
    assert dropper.is_alive()  # held behind the write in progress

    release.set()
    writer.join(5)
    dropper.join(5)

    assert store.record(SLUG) is None
    assert store.image(SLUG) is None


# ── render_preview: the pipeline up to the plate image ────────────────────────


async def test_a_preview_is_the_plate_image_of_a_render_at_the_default_parameters(
    paths: DataPaths,
) -> None:
    rendered_with: list[dict[str, object]] = []

    async def one_box(*args: object, **kwargs: object) -> object:
        rendered_with.append(dict(args[2]))  # type: ignore[call-overload]
        out = args[3]
        assert isinstance(out, Path)
        write_openscad_3mf(out, [("Color 1", "#0047BB00", trimesh.creation.box())])
        return mock.Mock(log_tail=[], missing_files=())

    async def cached_schema(*args: object, **kwargs: object) -> CustomizerSchema:
        return CustomizerSchema(parameters=[Parameter(name="width", type="number", initial=10)])

    with (
        mock.patch.object(previews_module, "render_3mf", one_box),
        mock.patch.object(previews_module, "cached_schema", cached_schema),
    ):
        png = await render_preview(
            SLUG, config=CONFIG, paths=paths, history=None, assets=AssetStore(paths.assets)
        )

    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    # Nothing passed: every parameter at the value the source declares.
    assert rendered_with == [{}]
    # Its scratch space is gone, and nothing was written beside the model.
    assert not any(paths.preview_work.iterdir())
    assert sorted(entry.name for entry in paths.model_dir(SLUG).iterdir()) == ["model.scad"]


async def test_a_default_render_with_no_geometry_is_a_failure(paths: DataPaths) -> None:
    async def nothing(*args: object, **kwargs: object) -> object:
        out = args[3]
        assert isinstance(out, Path)
        write_openscad_3mf(out, [])
        return mock.Mock(log_tail=[], missing_files=())

    async def cached_schema(*args: object, **kwargs: object) -> CustomizerSchema:
        return CustomizerSchema()

    with (
        mock.patch.object(previews_module, "render_3mf", nothing),
        mock.patch.object(previews_module, "cached_schema", cached_schema),
        pytest.raises(OpenSCADError, match="no geometry"),
    ):
        await render_preview(
            SLUG, config=CONFIG, paths=paths, history=None, assets=AssetStore(paths.assets)
        )
    assert not any(paths.preview_work.iterdir())


def _held_write(store: PreviewStore) -> tuple[threading.Thread, threading.Event, threading.Event]:
    """A write stopped inside the model's preview lock, at its "still wanted?" check."""
    checking = threading.Event()
    release = threading.Event()

    def wanted() -> bool:
        checking.set()
        release.wait(5)
        return True

    writer = threading.Thread(target=lambda: store.write(SLUG, "a" * 64, b"png", wanted=wanted))
    writer.start()
    assert checking.wait(5)
    return writer, checking, release


@pytest.mark.requires_postgres
@pytest.mark.parametrize("serving", [True, False], ids=["serving", "previews off"])
def test_a_reused_slugs_cleanup_waits_behind_a_write_in_progress(
    store: PreviewStore, paths: DataPaths, serving: bool
) -> None:
    """`_clear_derived` (a create or duplicate reusing the slug) takes the same lock as
    a render's write, whether or not previews are served, so the previous model's
    preview never survives it."""
    catalogue = Catalogue(paths, previews=store, serve_previews=serving)
    writer, _, release = _held_write(store)

    clearing = threading.Thread(target=lambda: catalogue._clear_derived(SLUG))
    clearing.start()
    clearing.join(0.2)
    assert clearing.is_alive()  # held behind the write in progress

    release.set()
    writer.join(5)
    clearing.join(5)
    assert store.record(SLUG) is None
    assert store.image(SLUG) is None


@pytest.mark.requires_postgres
def test_the_orphan_sweep_leaves_a_live_model_being_written(
    store: PreviewStore, paths: DataPaths
) -> None:
    store.write("gone", "a" * 64, b"png")
    writer, _, release = _held_write(store)

    sweeping = threading.Thread(target=Catalogue(paths, previews=store).sweep_orphan_previews)
    sweeping.start()
    sweeping.join(0.2)
    release.set()
    writer.join(5)
    sweeping.join(5)

    assert store.image("gone") is None
    assert store.image(SLUG) == b"png"


@pytest.mark.requires_postgres
def test_previews_outlive_the_process_and_are_locked_across_processes(
    pg_conninfo: str, paths: DataPaths
) -> None:
    """A second pool is a second process: it reads what the first wrote after the
    first is gone, and its drop waits behind the first one's write in progress."""
    first = PostgresJobStore(pg_conninfo, paths, pool_size=2)
    first.open()
    second = PostgresJobStore(pg_conninfo, paths, pool_size=2)
    second.open()
    try:
        mine, theirs = (
            PreviewStore(first.connection),
            PreviewStore(second.connection),
        )
        writer, _, release = _held_write(mine)
        dropper = threading.Thread(target=lambda: theirs.drop(SLUG))
        dropper.start()
        dropper.join(0.2)
        assert dropper.is_alive()
        release.set()
        writer.join(5)
        dropper.join(5)
        assert theirs.record(SLUG) is None

        mine.write(SLUG, "a" * 64, b"png")
    finally:
        first.close()
    try:
        assert PreviewStore(second.connection).image(SLUG) == b"png"
    finally:
        second.close()


@pytest.mark.requires_postgres
def test_a_row_has_its_image_exactly_when_it_rendered(pg_conninfo: str, paths: DataPaths) -> None:
    database = PostgresJobStore(pg_conninfo, paths, pool_size=1)
    database.open()
    database.close()
    insert = (
        "INSERT INTO model_previews (model_id, source_key, ok, png, rendered_at)"
        " VALUES (%s, 'k', %s, %s, now())"
    )
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        for slug, ok, png in (("rendered", True, None), ("failed", False, b"png")):
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(insert, (slug, ok, png))


async def test_a_preview_holds_a_lease_on_the_checkouts_it_renders_with(
    paths: DataPaths,
) -> None:
    """As a render job (#253): a library removal refuses while a preview reads the
    checkouts on its OPENSCADPATH, and the lease is gone once the render is done."""
    checkout = paths.libraries / "BOSL2" / ("a" * 40)
    (checkout / "BOSL2").mkdir(parents=True)
    gate = CheckoutGate()
    leased_during: list[list[str]] = []

    async def one_box(*args: object, **kwargs: object) -> object:
        leased_during.append(gate.leased(checkout))
        out = args[3]
        assert isinstance(out, Path)
        write_openscad_3mf(out, [("Color 1", "#0047BB00", trimesh.creation.box())])
        return mock.Mock(log_tail=[], missing_files=())

    async def cached_schema(*args: object, **kwargs: object) -> CustomizerSchema:
        return CustomizerSchema()

    async def resolved(*args: object, **kwargs: object) -> object:
        return ModelSource(
            scad=paths.model_source(SLUG),
            schema_cache=paths.model_schema_cache(SLUG),
            version=None,
            library_path=(checkout,),
        )

    with (
        mock.patch.object(previews_module, "render_3mf", one_box),
        mock.patch.object(previews_module, "cached_schema", cached_schema),
        mock.patch.object(previews_module, "resolve_source", resolved),
    ):
        await render_preview(
            SLUG,
            config=CONFIG,
            paths=paths,
            history=None,
            assets=AssetStore(paths.assets),
            checkouts=gate,
        )

    assert leased_during == [[f"preview:{SLUG}"]]
    assert gate.leased(checkout) == []
