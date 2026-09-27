"""Third-party OpenSCAD libraries as pinned git checkouts (#93), against real git.

Every upstream here is a local bare repository: the clone is the real one, the
network is never involved.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from scadbuddy.core.paths import DataPaths
from scadbuddy.library.catalogue import Catalogue, ModelMeta, ModelPatch
from scadbuddy.library.history import GitTimeoutError, ModelHistory
from scadbuddy.library.libraries import (
    LOCKFILE_NAME,
    CatalogueLibrary,
    LibraryError,
    LibraryFetchError,
    LibraryNotFoundError,
    LibraryNotInstalledError,
    LibraryPin,
    LibraryStore,
    _write_pins,
    declared_libraries,
    pins_at,
    read_pins,
    restore_pins,
    search_path,
)
from scadbuddy.render.jobs import resolve_source
from scadbuddy.render.solids import WRAPPER_PREFIX
from tests.conftest import PUBLIC_ADDRESS, make_library_upstream

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


def test_the_catalogue_url_matches_whatever_the_case_of_its_scheme(
    store: LibraryStore, upstream: tuple[str, dict[str, str]]
) -> None:
    url, commits = upstream
    scheme, rest = url.split("://", 1)

    pin = store.install("BOSL2", url=f"{scheme.upper()}://{rest}", ref="v2")

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


def _hold_the_first_clone(
    store: LibraryStore, monkeypatch: pytest.MonkeyPatch
) -> tuple[threading.Event, threading.Event]:
    """Park the first clone until released, so a second install can race it."""
    started, release = threading.Event(), threading.Event()
    clone = store._clone
    calls: list[str] = []

    def slow_clone(name: str, url: str, ref: str, pinned: Sequence[str] = ()) -> str:
        calls.append(name)
        if len(calls) == 1:
            started.set()
            assert release.wait(10)
        return clone(name, url, ref, pinned)

    monkeypatch.setattr(store, "_clone", slow_clone)
    return started, release


def test_concurrent_adds_of_one_new_name_cannot_both_bind_it(
    store: LibraryStore,
    paths: DataPaths,
    upstream: tuple[str, dict[str, str]],
    elsewhere: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both would pass the one-name-one-URL check before either had recorded a pin;
    the second must wait for the first and then see its URL."""
    url, _ = upstream
    started, release = _hold_the_first_clone(store, monkeypatch)
    errors: list[BaseException] = []

    def add_first() -> None:
        store.install("mylib", url=url, ref="v1")

    def add_second() -> None:
        try:
            store.install("mylib", url=elsewhere, ref="v1")
        except LibraryError as error:
            errors.append(error)

    first = threading.Thread(target=add_first)
    first.start()
    assert started.wait(10)
    second = threading.Thread(target=add_second)
    second.start()
    second.join(0.5)
    release.set()
    first.join(10)
    second.join(10)

    assert [str(error) for error in errors] == [
        f"'mylib' comes from {url}; add {elsewhere} under another name"
    ]
    assert read_pins(paths)["mylib"].url == url
    # The per-name lock is dropped once nobody holds or waits for it, so the table
    # does not grow with every name ever added -- or ever tried.
    assert store._names == {}


def test_the_lock_is_swapped_in_whole(paths: DataPaths, monkeypatch: pytest.MonkeyPatch) -> None:
    """Renders read the lock without the install lock, so a write that fails part
    way must leave the previous lock whole, and nothing staged behind."""
    paths.models.mkdir(parents=True, exist_ok=True)
    before = {"a": LibraryPin(url="https://example.invalid/a.git", ref="v1", commit="a" * 40)}
    _write_pins(paths, before)

    def fail(*_: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("scadbuddy.library.libraries.os.replace", fail)
    after = {"b": LibraryPin(url="https://example.invalid/b.git", ref="v2", commit="b" * 40)}
    with pytest.raises(OSError, match="disk full"):
        _write_pins(paths, after)

    assert read_pins(paths) == before
    assert [p.name for p in paths.models.iterdir() if p.name.startswith(".libraries-")] == []


def test_a_git_that_times_out_is_killed_with_its_helpers(
    paths: DataPaths, history: ModelHistory, tmp_path: Path
) -> None:
    """A clone runs git-remote-https as a child; a timeout must take it too, or a
    tarpit host keeps it alive after the request has given up."""
    child_pid = tmp_path / "child.pid"
    fake_git = tmp_path / "git"
    fake_git.write_text(
        f"#!/bin/sh\nsleep 60 & echo $! > {child_pid}\nwait\n",
        encoding="utf-8",
    )
    fake_git.chmod(0o755)
    store = LibraryStore(paths, history, catalogue=(), git=str(fake_git), timeout=0.5)

    with pytest.raises(LibraryFetchError, match="timed out"):
        store._git("ls-remote", "--", "https://git.example/o/r.git")

    pid = int(child_pid.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("the timed-out git's child is still running")


def test_a_refused_add_leaves_no_lock_behind(store: LibraryStore) -> None:
    with pytest.raises(LibraryNotFoundError):
        store.install("nothing-here")

    assert store._names == {}


def test_an_add_of_one_name_does_not_hold_up_another(
    store: LibraryStore,
    paths: DataPaths,
    upstream: tuple[str, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url, commits = upstream
    started, release = _hold_the_first_clone(store, monkeypatch)
    first = threading.Thread(target=lambda: store.install("mylib", url=url, ref="v1"))
    first.start()
    assert started.wait(10)

    try:
        assert store.install("BOSL2").commit == commits["v1"]
    finally:
        release.set()
        first.join(10)

    assert set(read_pins(paths)) == {"BOSL2", "mylib"}


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


def _recording_git(store: LibraryStore, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    """Every git call the store makes, each failing as an unreachable fetch would."""
    calls: list[tuple[str, ...]] = []

    def git(*args: str) -> str:
        calls.append(args)
        raise LibraryFetchError("unreachable")

    monkeypatch.setattr(store, "_git", git)
    return calls


@pytest.mark.parametrize(
    "addresses", [["127.0.0.1"], ["10.1.2.3"], ["::1"], [PUBLIC_ADDRESS, "169.254.169.254"]]
)
def test_a_url_whose_host_is_not_public_is_refused_without_running_git(
    paths: DataPaths,
    history: ModelHistory,
    fake_dns: dict[str, list[str]],
    monkeypatch: pytest.MonkeyPatch,
    addresses: list[str],
) -> None:
    fake_dns["git.internal.example"] = addresses
    https_only = LibraryStore(paths, history, catalogue=())
    calls = _recording_git(https_only, monkeypatch)

    with pytest.raises(LibraryError, match="public"):
        https_only.install("mylib", url="https://git.internal.example/o/r.git", ref="v1")

    assert calls == []
    assert not (paths.models / LOCKFILE_NAME).exists()


@pytest.mark.usefixtures("fake_dns")
def test_an_address_literal_that_is_not_public_is_refused(
    paths: DataPaths, history: ModelHistory, monkeypatch: pytest.MonkeyPatch
) -> None:
    https_only = LibraryStore(paths, history, catalogue=())
    calls = _recording_git(https_only, monkeypatch)

    with pytest.raises(LibraryError, match="public"):
        https_only.install("mylib", url="https://[::1]:8443/o/r.git", ref="v1")
    assert calls == []


@pytest.mark.parametrize(
    "url", ["https://tok3n@git.example/o/r.git", "https://me:s3cret@git.example/o/r.git"]
)
def test_a_url_carrying_credentials_is_refused_without_quoting_them(
    paths: DataPaths, history: ModelHistory, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    https_only = LibraryStore(paths, history, catalogue=())
    calls = _recording_git(https_only, monkeypatch)

    with pytest.raises(LibraryError, match="user name or password") as refused:
        https_only.install("mylib", url=url, ref="v1")
    assert "s3cret" not in str(refused.value) and "tok3n" not in str(refused.value)
    assert calls == []


@pytest.mark.usefixtures("fake_dns")
def test_a_public_url_is_cloned_from_only_the_addresses_it_was_vetted_at(
    paths: DataPaths, history: ModelHistory, monkeypatch: pytest.MonkeyPatch
) -> None:
    https_only = LibraryStore(paths, history, catalogue=())
    calls = _recording_git(https_only, monkeypatch)

    with pytest.raises(LibraryFetchError):
        https_only.install("mylib", url="https://git.example/o/r.git", ref="v1")

    (clone,) = calls
    assert clone[:2] == ("-c", f"http.curloptResolve=git.example:443:{PUBLIC_ADDRESS}")
    assert "clone" in clone


def test_a_catalogue_url_is_trusted_as_it_is(
    paths: DataPaths,
    history: ModelHistory,
    fake_dns: dict[str, list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_dns["git.internal.example"] = ["10.1.2.3"]
    curated = LibraryStore(
        paths,
        history,
        catalogue=(
            CatalogueLibrary(
                name="BOSL2",
                url="https://git.internal.example/o/BOSL2.git",
                ref="v1",
                licence="BSD-2-Clause",
                homepage="https://example.invalid/bosl2",
            ),
        ),
    )
    calls = _recording_git(curated, monkeypatch)

    with pytest.raises(LibraryFetchError):
        curated.install("BOSL2", url="https://GIT.internal.example/o/BOSL2/")

    (clone,) = calls
    assert "https://git.internal.example/o/BOSL2.git" in clone
    assert not any(arg.startswith("http.curloptResolve") for arg in clone)


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


def test_the_orphan_sweep_leaves_libraries_alone(
    store: LibraryStore,
    catalogue: Catalogue,
    paths: DataPaths,
    upstream: tuple[str, dict[str, str]],
) -> None:
    """Checkouts and the lockfile are not slug-keyed: the only model declaring a
    library being gone must not sweep either."""
    _, commits = upstream
    store.install("BOSL2")
    _declare_bosl2(catalogue)
    catalogue.delete("widget")

    catalogue.sweep_orphans()

    assert (paths.libraries / "BOSL2" / commits["v1"] / "BOSL2" / "std.scad").is_file()
    assert read_pins(paths)["BOSL2"].commit == commits["v1"]
