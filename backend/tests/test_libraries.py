"""Third-party OpenSCAD libraries as pinned git checkouts (#93), against real git.

Every upstream here is a local bare repository: the clone is the real one, the
network is never involved.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from scadbuddy.core.paths import DataPaths
from scadbuddy.library.catalogue import Catalogue, ModelMeta, ModelPatch
from scadbuddy.library.history import GitTimeoutError, ModelHistory
from scadbuddy.library.libraries import (
    LOCKFILE_NAME,
    CatalogueLibrary,
    LibraryError,
    LibraryNotFoundError,
    LibraryNotInstalledError,
    LibraryStore,
    declared_libraries,
    pins_at,
    read_pins,
    restore_pins,
    search_path,
)
from scadbuddy.render.jobs import resolve_source
from scadbuddy.render.solids import WRAPPER_PREFIX
from tests.conftest import make_library_upstream

pytestmark = pytest.mark.requires_git

V1 = "module marker() cube(1);\n"
V2 = "module marker() cube(2);\n"


@pytest.fixture
def upstream(tmp_path: Path) -> tuple[str, dict[str, str]]:
    return make_library_upstream(tmp_path, {"v1": V1, "v2": V2})


@pytest.fixture
def paths(tmp_path: Path) -> DataPaths:
    data = DataPaths(tmp_path / "data")
    data.ensure()
    return data


@pytest.fixture
def history(paths: DataPaths) -> ModelHistory:
    repository = ModelHistory(paths.models, wrapper_prefix=WRAPPER_PREFIX)
    repository.ensure_repo()
    return repository


@pytest.fixture
def store(
    paths: DataPaths, history: ModelHistory, upstream: tuple[str, dict[str, str]]
) -> LibraryStore:
    url, _ = upstream
    return LibraryStore(
        paths,
        history,
        catalogue=(
            CatalogueLibrary(
                name="BOSL2",
                url=url,
                ref="v1",
                licence="BSD-2-Clause",
                homepage="https://example.invalid/bosl2",
            ),
        ),
        protocols=("file",),
    )


def test_install_clones_the_catalogue_default_and_records_the_commit(
    store: LibraryStore,
    paths: DataPaths,
    history: ModelHistory,
    upstream: tuple[str, dict[str, str]],
) -> None:
    url, commits = upstream

    pin = store.install("BOSL2")

    assert (pin.url, pin.ref, pin.commit) == (url, "v1", commits["v1"])
    # Laid out so `use <BOSL2/std.scad>` resolves with the parent on OPENSCADPATH.
    checkout = paths.libraries / "BOSL2" / commits["v1"] / "BOSL2" / "std.scad"
    assert checkout.read_text(encoding="utf-8") == V1
    lock = json.loads((paths.models / LOCKFILE_NAME).read_text(encoding="utf-8"))
    assert lock == {"BOSL2": {"url": url, "ref": "v1", "commit": commits["v1"]}}
    # The pin is a revision of the models repository, not a file beside it.
    latest = history.log(limit=1)[0]
    assert latest.message == f"Pin BOSL2 to v1 ({commits['v1'][:7]})"
    assert [change.path for change in latest.files] == [LOCKFILE_NAME]


def test_a_new_ref_moves_the_pin_and_keeps_the_old_checkout(
    store: LibraryStore,
    paths: DataPaths,
    history: ModelHistory,
    upstream: tuple[str, dict[str, str]],
) -> None:
    _, commits = upstream
    store.install("BOSL2")

    pin = store.install("BOSL2", ref="v2")

    assert pin.commit == commits["v2"]
    assert read_pins(paths)["BOSL2"].commit == commits["v2"]
    # An old revision still renders against the pin it was written with.
    assert (paths.libraries / "BOSL2" / commits["v1"] / "BOSL2").is_dir()
    assert (paths.libraries / "BOSL2" / commits["v2"] / "BOSL2").is_dir()
    assert [r.message for r in history.log(limit=2)] == [
        f"Pin BOSL2 to v2 ({commits['v2'][:7]})",
        f"Pin BOSL2 to v1 ({commits['v1'][:7]})",
    ]


def test_reinstalling_the_same_pin_is_not_a_revision(
    store: LibraryStore, history: ModelHistory
) -> None:
    store.install("BOSL2")
    head = history.head()

    store.install("BOSL2")

    assert history.head() == head


def test_a_failed_commit_does_not_fail_a_pin_that_was_written(
    store: LibraryStore,
    paths: DataPaths,
    history: ModelHistory,
    monkeypatch: pytest.MonkeyPatch,
    upstream: tuple[str, dict[str, str]],
) -> None:
    """As a catalogue action: the lockfile is what renders read, so the pin is live
    and the client must not be told it failed."""
    _, commits = upstream
    head = history.head()

    def fail_after_prepare(
        message: str, *paths_: str, prepare: Callable[[], None] | None = None
    ) -> str | None:
        assert prepare is not None
        prepare()
        raise GitTimeoutError("git commit timed out after 30s")

    monkeypatch.setattr(history, "commit", fail_after_prepare)

    pin = store.install("BOSL2")

    assert pin.commit == commits["v1"]
    assert read_pins(paths)["BOSL2"] == pin
    assert history.head() == head


def test_a_commit_that_failed_before_writing_still_fails(
    store: LibraryStore, history: ModelHistory, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_on_the_lock(
        message: str, *paths_: str, prepare: Callable[[], None] | None = None
    ) -> str | None:
        raise GitTimeoutError("waited 30s for the in-process write lock")

    monkeypatch.setattr(history, "commit", fail_on_the_lock)

    with pytest.raises(GitTimeoutError):
        store.install("BOSL2")


def test_a_user_added_library_is_recorded_with_its_url(
    store: LibraryStore, paths: DataPaths, upstream: tuple[str, dict[str, str]]
) -> None:
    url, commits = upstream

    store.install("mylib", url=url, ref="v2")

    assert read_pins(paths)["mylib"].url == url
    entries = {entry.name: entry for entry in store.entries()}
    assert entries["mylib"].curated is False
    assert entries["mylib"].pin is not None
    assert entries["mylib"].pin.commit == commits["v2"]
    assert entries["BOSL2"].curated is True
    assert entries["BOSL2"].pin is None


@pytest.fixture
def elsewhere(tmp_path: Path) -> str:
    """A second upstream, standing in for someone else's repository."""
    url, _ = make_library_upstream(tmp_path / "elsewhere", {"v1": "module marker() sphere(1);\n"})
    return url


def test_a_curated_name_cannot_be_pointed_at_another_repository(
    store: LibraryStore, paths: DataPaths, elsewhere: str
) -> None:
    """Every model declaring BOSL2 trusts the catalogue's upstream; a caller must not
    be able to swap that out by naming BOSL2 with its own URL."""
    with pytest.raises(LibraryError, match="BOSL2"):
        store.install("BOSL2", url=elsewhere, ref="v1")

    assert not (paths.models / LOCKFILE_NAME).exists()
    assert list(paths.libraries.iterdir()) == []


@pytest.mark.parametrize("suffix", ["/", ".git", ".git/"])
def test_the_catalogue_url_matches_however_it_is_spelled(
    store: LibraryStore, paths: DataPaths, upstream: tuple[str, dict[str, str]], suffix: str
) -> None:
    url, commits = upstream
    spelled = url.removesuffix(".git") + suffix

    pin = store.install("BOSL2", url=spelled, ref="v2")

    # Recorded as the catalogue spells it, so the lock never carries two spellings.
    assert (pin.url, pin.commit) == (url, commits["v2"])


def test_a_user_added_library_cannot_be_repointed(
    store: LibraryStore, paths: DataPaths, upstream: tuple[str, dict[str, str]], elsewhere: str
) -> None:
    url, commits = upstream
    store.install("mylib", url=url, ref="v1")

    with pytest.raises(LibraryError, match="mylib"):
        store.install("mylib", url=elsewhere, ref="v1")

    assert read_pins(paths)["mylib"].url == url
    # Its own URL still moves it to another ref.
    assert store.install("mylib", url=url, ref="v2").commit == commits["v2"]


def test_a_name_outside_the_catalogue_needs_a_url(store: LibraryStore) -> None:
    with pytest.raises(LibraryNotFoundError):
        store.install("nothing-here")


def test_a_user_added_library_needs_a_ref(
    store: LibraryStore, upstream: tuple[str, dict[str, str]]
) -> None:
    url, _ = upstream
    with pytest.raises(LibraryError, match="ref"):
        store.install("mylib", url=url)


def test_only_the_allowed_transports_are_cloned(
    paths: DataPaths, history: ModelHistory, upstream: tuple[str, dict[str, str]]
) -> None:
    url, _ = upstream
    https_only = LibraryStore(paths, history, catalogue=())

    with pytest.raises(LibraryError, match="https"):
        https_only.install("mylib", url=url, ref="v1")
    assert not (paths.models / LOCKFILE_NAME).exists()


@pytest.mark.parametrize("name", ["../escape", ".hidden", "a/b", ""])
def test_a_name_that_is_not_a_directory_name_is_refused(
    store: LibraryStore, upstream: tuple[str, dict[str, str]], name: str
) -> None:
    url, _ = upstream
    with pytest.raises(LibraryError):
        store.install(name, url=url, ref="v1")


@pytest.mark.parametrize("ref", ["-upload-pack=x", "a..b", "v1 v2"])
def test_a_ref_that_is_not_a_ref_is_refused(store: LibraryStore, ref: str) -> None:
    with pytest.raises(LibraryError):
        store.install("BOSL2", ref=ref)


def test_an_unknown_ref_leaves_nothing_behind(store: LibraryStore, paths: DataPaths) -> None:
    with pytest.raises(LibraryError, match="clone"):
        store.install("BOSL2", ref="v9")

    assert not (paths.models / LOCKFILE_NAME).exists()
    assert list(paths.libraries.iterdir()) == []


def test_search_path_is_only_what_the_model_declares(
    store: LibraryStore, paths: DataPaths, upstream: tuple[str, dict[str, str]]
) -> None:
    url, commits = upstream
    store.install("BOSL2")
    store.install("other", url=url, ref="v2")
    pins = read_pins(paths)

    assert search_path(paths, ["BOSL2"], pins) == (paths.libraries / "BOSL2" / commits["v1"],)
    assert search_path(paths, [], pins) == ()


def test_declaring_a_library_that_is_not_pinned_is_an_error(paths: DataPaths) -> None:
    with pytest.raises(LibraryNotInstalledError, match="BOSL2"):
        search_path(paths, ["BOSL2"], {})


def test_a_pin_whose_checkout_is_gone_is_an_error(
    store: LibraryStore, paths: DataPaths, upstream: tuple[str, dict[str, str]]
) -> None:
    _, commits = upstream
    store.install("BOSL2")
    (paths.libraries / "BOSL2" / commits["v1"] / "BOSL2" / "std.scad").unlink()
    (paths.libraries / "BOSL2" / commits["v1"] / "BOSL2").rename(paths.root / "moved")

    with pytest.raises(LibraryNotInstalledError, match="BOSL2"):
        search_path(paths, ["BOSL2"], read_pins(paths))


def test_pins_at_reads_the_lock_as_it_was_at_a_revision(
    store: LibraryStore, history: ModelHistory, upstream: tuple[str, dict[str, str]]
) -> None:
    _, commits = upstream
    before_any = history.head()
    assert before_any is not None
    store.install("BOSL2")
    first = history.head()
    assert first is not None
    store.install("BOSL2", ref="v2")

    assert pins_at(history, before_any) == {}
    assert pins_at(history, first)["BOSL2"].commit == commits["v1"]


def test_declared_libraries_reads_model_json(tmp_path: Path) -> None:
    (tmp_path / "model.json").write_text(
        json.dumps({"name": "x", "libraries": ["BOSL2", "dotSCAD"]}), encoding="utf-8"
    )
    assert declared_libraries(tmp_path) == ["BOSL2", "dotSCAD"]
    assert declared_libraries(tmp_path / "missing") == []


# ── a model and its pins, versioned together ──────────────────────────────────


@pytest.fixture
def catalogue(paths: DataPaths, history: ModelHistory) -> Catalogue:
    return Catalogue(paths, history)


def _declare_bosl2(catalogue: Catalogue) -> None:
    catalogue.create("widget", "use <BOSL2/std.scad>\nmarker();\n", ModelMeta(name="Widget"))
    catalogue.update("widget", ModelPatch(libraries=["BOSL2"]))


def test_restoring_a_revision_restores_its_pins(
    store: LibraryStore,
    catalogue: Catalogue,
    history: ModelHistory,
    paths: DataPaths,
    upstream: tuple[str, dict[str, str]],
) -> None:
    _, commits = upstream
    store.install("BOSL2")
    _declare_bosl2(catalogue)
    written_against = catalogue.version("widget")
    assert written_against is not None
    catalogue.write_source("widget", "use <BOSL2/std.scad>\nmarker();\ncube(3);\n")
    store.install("BOSL2", ref="v2")

    restored = history.restore(
        "widget",
        written_against,
        also=lambda commit: restore_pins(history, paths, "widget", commit),
    )

    assert read_pins(paths)["BOSL2"].commit == commits["v1"]
    # One revision: the model and the pin it goes with.
    latest = history.log(limit=1)[0]
    assert latest.commit == restored
    assert sorted(change.path for change in latest.files) == [LOCKFILE_NAME, "widget/model.scad"]


def test_restoring_leaves_pins_the_model_does_not_declare(
    store: LibraryStore,
    catalogue: Catalogue,
    history: ModelHistory,
    paths: DataPaths,
    upstream: tuple[str, dict[str, str]],
) -> None:
    url, commits = upstream
    store.install("BOSL2")
    store.install("other", url=url, ref="v1")
    _declare_bosl2(catalogue)
    written_against = catalogue.version("widget")
    assert written_against is not None
    store.install("other", url=url, ref="v2")

    history.restore(
        "widget",
        written_against,
        also=lambda commit: restore_pins(history, paths, "widget", commit),
    )

    assert read_pins(paths)["other"].commit == commits["v2"]


async def test_a_render_resolves_the_live_pins(
    store: LibraryStore,
    catalogue: Catalogue,
    history: ModelHistory,
    paths: DataPaths,
    upstream: tuple[str, dict[str, str]],
) -> None:
    _, commits = upstream
    store.install("BOSL2")
    _declare_bosl2(catalogue)

    source = await resolve_source("widget", None, paths=paths, history=history)

    assert source.library_path == (paths.libraries / "BOSL2" / commits["v1"],)


async def test_an_old_revision_renders_against_the_pins_it_was_written_with(
    store: LibraryStore,
    catalogue: Catalogue,
    history: ModelHistory,
    paths: DataPaths,
    upstream: tuple[str, dict[str, str]],
) -> None:
    _, commits = upstream
    store.install("BOSL2")
    _declare_bosl2(catalogue)
    written_against = catalogue.version("widget")
    assert written_against is not None
    catalogue.write_source("widget", "use <BOSL2/std.scad>\nmarker();\ncube(3);\n")
    store.install("BOSL2", ref="v2")

    old = await resolve_source("widget", written_against, paths=paths, history=history)
    live = await resolve_source("widget", None, paths=paths, history=history)

    assert old.library_path == (paths.libraries / "BOSL2" / commits["v1"],)
    assert live.library_path == (paths.libraries / "BOSL2" / commits["v2"],)
