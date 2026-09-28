"""The default-render preview store (#179 follow-up) and its place in the catalogue."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest import mock

import pytest
import trimesh

from scadbuddy.core.config import Config
from scadbuddy.core.paths import DataPaths
from scadbuddy.library.assets import AssetStore
from scadbuddy.library.catalogue import Catalogue
from scadbuddy.library.libraries import CheckoutGate
from scadbuddy.library.previews import PREVIEW_ID_LENGTH, PreviewStore, source_key
from scadbuddy.render import previews as previews_module
from scadbuddy.render.jobs import ModelSource
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


def test_a_preview_is_served_only_once_rendered_and_a_failure_serves_nothing(
    paths: DataPaths,
) -> None:
    store = PreviewStore(paths)
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


def test_an_unreadable_record_is_no_preview(paths: DataPaths) -> None:
    store = PreviewStore(paths)
    store.write(SLUG, "a" * 64, b"png")
    paths.model_preview_record(SLUG).write_text("{not json", encoding="utf-8")

    assert store.record(SLUG) is None
    assert store.preview_id(SLUG) is None


def test_the_orphan_sweep_takes_a_gone_models_preview_but_not_a_write_in_flight(
    paths: DataPaths,
) -> None:
    store = PreviewStore(paths)
    store.write("gone", "a" * 64, b"png")
    store.write(SLUG, "a" * 64, b"png")
    in_flight = paths.previews / ".gone-abc123.png"
    in_flight.write_bytes(b"half")

    removed = Catalogue(paths).sweep_orphans()

    assert sorted(removed) == ["cache/previews/gone.json", "cache/previews/gone.png"]
    assert store.image(SLUG) == b"png"
    assert in_flight.exists()


def test_the_catalogue_ranks_its_own_image_over_the_preview(paths: DataPaths) -> None:
    store = PreviewStore(paths)
    catalogue = Catalogue(paths, previews=store)
    store.write(SLUG, "a" * 64, b"preview")

    origin = catalogue.thumbnail_source(SLUG)
    assert (origin.source, origin.preview_id) == ("preview", "a" * PREVIEW_ID_LENGTH)
    assert catalogue.thumbnail(SLUG) == b"preview"

    catalogue.write_thumbnail(SLUG, b"own")
    assert catalogue.thumbnail_source(SLUG).source == "model"
    assert catalogue.thumbnail(SLUG) == b"own"
    assert store.image(SLUG) is None


def test_a_write_no_longer_wanted_writes_nothing(paths: DataPaths) -> None:
    store = PreviewStore(paths)

    assert store.write(SLUG, "a" * 64, b"png", wanted=lambda: False) is False
    assert store.record_failure(SLUG, "a" * 64, "boom", wanted=lambda: False) is False
    assert store.record(SLUG) is None
    assert store.image(SLUG) is None


def test_a_record_whose_image_is_gone_is_not_current(paths: DataPaths) -> None:
    store = PreviewStore(paths)
    store.write(SLUG, "a" * 64, b"png")
    assert store.current(SLUG, "a" * 64)

    paths.model_preview(SLUG).unlink()

    assert not store.current(SLUG, "a" * 64)
    # A failure never had an image; it stays current, so it is not retried.
    store.record_failure(SLUG, "b" * 64, "boom")
    assert store.current(SLUG, "b" * 64)
    assert not store.current(SLUG, "c" * 64)


def test_a_drop_cannot_land_between_the_check_and_the_write(paths: DataPaths) -> None:
    """The render's liveness check and its write are one step against a drop: a drop
    that arrives mid-write waits, then removes both files -- never just the image."""
    store = PreviewStore(paths)
    checking = threading.Event()
    release = threading.Event()

    def wanted() -> bool:
        checking.set()
        release.wait(5)
        return True

    writer = threading.Thread(target=lambda: store.write(SLUG, "a" * 64, b"png", wanted=wanted))
    writer.start()
    assert checking.wait(5)
    dropper = threading.Thread(target=lambda: store.drop(SLUG))
    dropper.start()
    dropper.join(0.1)
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
    assert not any(paths.previews.iterdir())
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
    assert not any(paths.previews.iterdir())


def _held_write(store: PreviewStore) -> tuple[threading.Thread, threading.Event, threading.Event]:
    """A write stopped inside the preview lock, at its "still wanted?" check."""
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


@pytest.mark.parametrize("attached", [True, False], ids=["store attached", "previews off"])
def test_a_reused_slugs_cleanup_waits_behind_a_write_in_progress(
    paths: DataPaths, attached: bool
) -> None:
    """`_clear_derived` (a create or duplicate reusing the slug) takes the same lock as
    a render's write, with or without a store attached, so it removes both files or
    neither -- never the image alone."""
    store = PreviewStore(paths)
    catalogue = Catalogue(paths, previews=store if attached else None)
    writer, _, release = _held_write(store)

    clearing = threading.Thread(target=lambda: catalogue._clear_derived(SLUG))
    clearing.start()
    clearing.join(0.1)
    assert clearing.is_alive()  # held behind the write in progress

    release.set()
    writer.join(5)
    clearing.join(5)
    assert store.record(SLUG) is None
    assert store.image(SLUG) is None


def test_the_orphan_sweep_waits_behind_a_write_in_progress(paths: DataPaths) -> None:
    store = PreviewStore(paths)
    store.write("gone", "a" * 64, b"png")
    writer, _, release = _held_write(store)

    sweeping = threading.Thread(target=lambda: Catalogue(paths).sweep_orphans())
    sweeping.start()
    sweeping.join(0.1)
    assert sweeping.is_alive()

    release.set()
    writer.join(5)
    sweeping.join(5)
    assert store.image("gone") is None
    assert store.image(SLUG) == b"png"


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
