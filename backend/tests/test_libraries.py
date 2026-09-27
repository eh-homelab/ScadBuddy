"""Third-party OpenSCAD libraries as pinned git checkouts (#93), pinned per model,
against real git.

Every upstream here is a local bare repository: the clone is the real one, the
network is never involved.
"""

from __future__ import annotations

import json
import shutil
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from scadbuddy.core.paths import DataPaths
from scadbuddy.library.catalogue import Catalogue, LibraryNotDeclaredError, ModelMeta
from scadbuddy.library.history import ModelHistory
from scadbuddy.library.libraries import (
    LOCKFILE_NAME,
    STAGING_PREFIX,
    CatalogueLibrary,
    LibraryDeclarationError,
    LibraryError,
    LibraryFetchError,
    LibraryNotFoundError,
    LibraryNotInstalledError,
    LibraryPin,
    LibraryStore,
    Lock,
    ModelLibrary,
    declared_libraries,
    lock_at,
    migrate_lockfile,
    model_search_path,
    pin_restored_declaration,
    read_lock,
    search_path,
)
from scadbuddy.render.jobs import resolve_source
from scadbuddy.render.solids import WRAPPER_PREFIX
from tests.conftest import PUBLIC_ADDRESS, make_library_upstream

pytestmark = pytest.mark.requires_git

V1 = "module marker() cube(1);\n"
V2 = "module marker() cube(2);\n"
SOURCE = "use <BOSL2/std.scad>\nmarker();\n"


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
def store(paths: DataPaths, upstream: tuple[str, dict[str, str]]) -> LibraryStore:
    url, _ = upstream
    return LibraryStore(
        paths,
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


@pytest.fixture
def catalogue(paths: DataPaths, history: ModelHistory) -> Catalogue:
    return Catalogue(paths, history)


def _create(catalogue: Catalogue, slug: str = "widget") -> None:
    catalogue.create(slug, SOURCE, ModelMeta(name=slug.title()))


# ── fetching a pin ────────────────────────────────────────────────────────────


def test_resolve_clones_the_catalogue_default_and_records_nothing(
    store: LibraryStore,
    paths: DataPaths,
    history: ModelHistory,
    upstream: tuple[str, dict[str, str]],
) -> None:
    url, commits = upstream
    head = history.head()

    pin = store.resolve("BOSL2")

    assert pin == ModelLibrary(name="BOSL2", url=url, ref="v1", commit=commits["v1"])
    # Laid out so `use <BOSL2/std.scad>` resolves with the parent on OPENSCADPATH.
    checkout = paths.libraries / "BOSL2" / commits["v1"] / "BOSL2" / "std.scad"
    assert checkout.read_text(encoding="utf-8") == V1
    # The pin is the model's to record; fetching it is no revision, and no lockfile.
    assert history.head() == head
    assert not (paths.models / LOCKFILE_NAME).exists()


def test_a_new_ref_is_a_second_checkout_beside_the_first(
    store: LibraryStore, paths: DataPaths, upstream: tuple[str, dict[str, str]]
) -> None:
    _, commits = upstream

    store.resolve("BOSL2")
    assert store.resolve("BOSL2", ref="v2").commit == commits["v2"]

    assert sorted(entry.name for entry in (paths.libraries / "BOSL2").iterdir()) == sorted(
        [commits["v1"], commits["v2"]]
    )


def test_a_clone_that_finds_its_commit_already_there_keeps_that_checkout(
    store: LibraryStore, paths: DataPaths, upstream: tuple[str, dict[str, str]]
) -> None:
    """The rename onto an existing checkout fails; the tree there is the same commit,
    so the clone gives way to it and leaves no staging directory behind."""
    _, commits = upstream
    first = store.resolve("BOSL2")
    checkout = paths.libraries / "BOSL2" / commits["v1"] / "BOSL2"
    marker = checkout / ".first"
    marker.write_text("", encoding="utf-8")

    second = store.resolve("BOSL2")

    assert second == first
    assert marker.is_file()
    assert [entry.name for entry in paths.libraries.iterdir()] == ["BOSL2"]


def test_concurrent_clones_of_one_commit_both_succeed(
    store: LibraryStore, paths: DataPaths, upstream: tuple[str, dict[str, str]]
) -> None:
    _, commits = upstream
    barrier = threading.Barrier(2)
    results: list[ModelLibrary] = []
    errors: list[BaseException] = []

    def resolve() -> None:
        barrier.wait(10)
        try:
            results.append(store.resolve("BOSL2"))
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=resolve) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    assert errors == []
    assert [pin.commit for pin in results] == [commits["v1"]] * 2
    checkout = paths.libraries / "BOSL2" / commits["v1"] / "BOSL2" / "std.scad"
    assert checkout.read_text(encoding="utf-8") == V1
    assert [entry.name for entry in paths.libraries.iterdir()] == ["BOSL2"]


def test_the_catalogue_is_what_can_be_suggested(store: LibraryStore) -> None:
    assert [entry.name for entry in store.entries()] == ["BOSL2"]


@pytest.fixture
def elsewhere(tmp_path: Path) -> str:
    """A second upstream, standing in for a fork."""
    url, _ = make_library_upstream(tmp_path / "elsewhere", {"v1": "module marker() sphere(1);\n"})
    return url


def test_a_curated_name_can_be_pinned_from_a_fork(
    store: LibraryStore, elsewhere: str, paths: DataPaths
) -> None:
    """A fork of BOSL2 is still `use <BOSL2/...>`. The pin is one model's, so it
    swaps nothing out from under any other."""
    pin = store.resolve("BOSL2", url=elsewhere, ref="v1")

    assert pin.url == elsewhere
    assert (paths.libraries / "BOSL2" / pin.commit / "BOSL2" / "std.scad").read_text(
        encoding="utf-8"
    ) == "module marker() sphere(1);\n"


def test_a_fork_of_a_curated_name_needs_a_ref(store: LibraryStore, elsewhere: str) -> None:
    with pytest.raises(LibraryError, match="ref"):
        store.resolve("BOSL2", url=elsewhere)


@pytest.mark.parametrize("suffix", ["/", ".git", ".git/"])
def test_the_catalogue_url_matches_however_it_is_spelled(
    store: LibraryStore, upstream: tuple[str, dict[str, str]], suffix: str
) -> None:
    url, commits = upstream
    spelled = url.removesuffix(".git") + suffix

    pin = store.resolve("BOSL2", url=spelled, ref="v2")

    # Recorded as the catalogue spells it, so pins never carry two spellings.
    assert (pin.url, pin.commit) == (url, commits["v2"])


def test_the_catalogue_url_matches_whatever_the_case_of_its_scheme(
    store: LibraryStore, upstream: tuple[str, dict[str, str]]
) -> None:
    url, commits = upstream
    scheme, rest = url.split("://", 1)

    pin = store.resolve("BOSL2", url=f"{scheme.upper()}://{rest}")

    # The catalogue's own URL, so its default ref too.
    assert (pin.url, pin.ref, pin.commit) == (url, "v1", commits["v1"])


def test_a_user_named_library_is_pinned_from_its_url(
    store: LibraryStore, upstream: tuple[str, dict[str, str]]
) -> None:
    url, commits = upstream

    pin = store.resolve("mylib", url=url, ref="v2")

    assert (pin.name, pin.url, pin.commit) == ("mylib", url, commits["v2"])


def test_a_name_outside_the_catalogue_needs_a_url(store: LibraryStore) -> None:
    with pytest.raises(LibraryNotFoundError):
        store.resolve("nothing-here")


def test_a_user_added_library_needs_a_ref(
    store: LibraryStore, upstream: tuple[str, dict[str, str]]
) -> None:
    url, _ = upstream
    with pytest.raises(LibraryError, match="ref"):
        store.resolve("mylib", url=url)


def test_only_the_allowed_transports_are_cloned(
    paths: DataPaths, history: ModelHistory, upstream: tuple[str, dict[str, str]]
) -> None:
    url, _ = upstream
    https_only = LibraryStore(paths, catalogue=())

    with pytest.raises(LibraryError, match="https"):
        https_only.resolve("mylib", url=url, ref="v1")


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
    https_only = LibraryStore(paths, catalogue=())
    calls = _recording_git(https_only, monkeypatch)

    with pytest.raises(LibraryError, match="public"):
        https_only.resolve("mylib", url="https://git.internal.example/o/r.git", ref="v1")

    assert calls == []


@pytest.mark.usefixtures("fake_dns")
def test_an_address_literal_that_is_not_public_is_refused(
    paths: DataPaths, history: ModelHistory, monkeypatch: pytest.MonkeyPatch
) -> None:
    https_only = LibraryStore(paths, catalogue=())
    calls = _recording_git(https_only, monkeypatch)

    with pytest.raises(LibraryError, match="public"):
        https_only.resolve("mylib", url="https://[::1]:8443/o/r.git", ref="v1")
    assert calls == []


@pytest.mark.parametrize(
    "url", ["https://tok3n@git.example/o/r.git", "https://me:s3cret@git.example/o/r.git"]
)
def test_a_url_carrying_credentials_is_refused_without_quoting_them(
    paths: DataPaths, history: ModelHistory, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    https_only = LibraryStore(paths, catalogue=())
    calls = _recording_git(https_only, monkeypatch)

    with pytest.raises(LibraryError, match="user name or password") as refused:
        https_only.resolve("mylib", url=url, ref="v1")
    assert "s3cret" not in str(refused.value) and "tok3n" not in str(refused.value)
    assert calls == []


@pytest.mark.usefixtures("fake_dns")
def test_a_public_url_is_cloned_from_only_the_addresses_it_was_vetted_at(
    paths: DataPaths, history: ModelHistory, monkeypatch: pytest.MonkeyPatch
) -> None:
    https_only = LibraryStore(paths, catalogue=())
    calls = _recording_git(https_only, monkeypatch)

    with pytest.raises(LibraryFetchError):
        https_only.resolve("mylib", url="https://git.example/o/r.git", ref="v1")

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
        curated.resolve("BOSL2", url="https://GIT.internal.example/o/BOSL2/")

    (clone,) = calls
    assert "https://git.internal.example/o/BOSL2.git" in clone
    assert not any(arg.startswith("http.curloptResolve") for arg in clone)


@pytest.mark.parametrize("name", ["../escape", ".hidden", "a/b", ""])
def test_a_name_that_is_not_a_directory_name_is_refused(
    store: LibraryStore, upstream: tuple[str, dict[str, str]], name: str
) -> None:
    url, _ = upstream
    with pytest.raises(LibraryError):
        store.resolve(name, url=url, ref="v1")


@pytest.mark.parametrize("ref", ["-upload-pack=x", "a..b", "v1 v2"])
def test_a_ref_that_is_not_a_ref_is_refused(store: LibraryStore, ref: str) -> None:
    with pytest.raises(LibraryError):
        store.resolve("BOSL2", ref=ref)


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
    store = LibraryStore(paths, catalogue=(), git=str(fake_git), timeout=0.5)

    with pytest.raises(LibraryFetchError, match="timed out"):
        store._git("ls-remote", "--", "https://git.example/o/r.git")

    pid = int(child_pid.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not _running(pid):
            break
        time.sleep(0.05)
    else:
        pytest.fail("the timed-out git's child is still running")


def _running(pid: int) -> bool:
    """A killed orphan whose new parent never reaps it (pytest as PID 1 in the test
    image) stays a zombie: dead, but still answering `kill(pid, 0)`."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:  # gone, or going: ENOENT or ESRCH mid-read
        return False
    return stat.rsplit(")", 1)[1].split()[0] != "Z"


def test_sweep_staging_leaves_the_checkouts(store: LibraryStore, paths: DataPaths) -> None:
    store.resolve("BOSL2")
    (paths.libraries / f"{STAGING_PREFIX}dead" / "BOSL2").mkdir(parents=True)

    assert store.sweep_staging() == [f"{STAGING_PREFIX}dead"]
    assert [entry.name for entry in paths.libraries.iterdir()] == ["BOSL2"]


def test_sweep_staging_goes_on_past_one_it_cannot_remove(
    store: LibraryStore,
    paths: DataPaths,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    stuck, gone = (paths.libraries / f"{STAGING_PREFIX}{tag}" for tag in ("a", "b"))
    for staging in (stuck, gone):
        (staging / "BOSL2").mkdir(parents=True)
    real_rmtree = shutil.rmtree

    def rmtree(path: Path, *args: Any, **kwargs: Any) -> None:
        if Path(path) == stuck:
            raise PermissionError("EACCES")
        real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", rmtree)

    assert store.sweep_staging() == [gone.name]
    assert stuck.is_dir()
    assert not gone.exists()
    assert "could not remove a staging clone" in caplog.text


@pytest.mark.parametrize("commit", ["a" * 40, "b" * 64])
def test_a_pin_takes_a_sha1_or_sha256_commit(commit: str) -> None:
    assert LibraryPin(url="https://x.invalid/b.git", ref="v1", commit=commit).commit == commit


def test_an_unknown_ref_leaves_nothing_behind(store: LibraryStore, paths: DataPaths) -> None:
    with pytest.raises(LibraryError, match="clone"):
        store.resolve("BOSL2", ref="v9")

    assert list(paths.libraries.iterdir()) == []


# ── a model's pins ────────────────────────────────────────────────────────────


def test_pinning_writes_the_pin_into_that_model_alone(
    store: LibraryStore,
    catalogue: Catalogue,
    history: ModelHistory,
    paths: DataPaths,
    upstream: tuple[str, dict[str, str]],
) -> None:
    url, commits = upstream
    _create(catalogue, "widget")
    _create(catalogue, "gadget")

    record = catalogue.pin_library("widget", store.resolve("BOSL2"))

    pinned = {"name": "BOSL2", "url": url, "ref": "v1", "commit": commits["v1"]}
    assert [library.model_dump() for library in record.libraries] == [pinned]
    stored = json.loads(paths.model_meta("widget").read_text(encoding="utf-8"))
    assert stored["libraries"] == [pinned]
    assert catalogue.record("gadget").libraries == []
    # One revision of that model, and nothing outside it.
    latest = history.log(limit=1)[0]
    assert latest.message == f"Pin BOSL2 to v1 ({commits['v1'][:7]}) for widget"
    assert [change.path for change in latest.files] == ["widget/model.json"]


def test_two_models_pin_one_library_at_two_refs(
    store: LibraryStore,
    catalogue: Catalogue,
    paths: DataPaths,
    upstream: tuple[str, dict[str, str]],
) -> None:
    _, commits = upstream
    _create(catalogue, "widget")
    _create(catalogue, "gadget")
    catalogue.pin_library("widget", store.resolve("BOSL2"))
    catalogue.pin_library("gadget", store.resolve("BOSL2", ref="v2"))

    assert search_path(paths, declared_libraries(paths.model_dir("widget"))) == (
        paths.libraries / "BOSL2" / commits["v1"],
    )
    assert search_path(paths, declared_libraries(paths.model_dir("gadget"))) == (
        paths.libraries / "BOSL2" / commits["v2"],
    )


def test_re_pinning_replaces_the_entry_in_place(
    store: LibraryStore, catalogue: Catalogue, upstream: tuple[str, dict[str, str]]
) -> None:
    url, commits = upstream
    _create(catalogue)
    catalogue.pin_library("widget", store.resolve("BOSL2"))
    catalogue.pin_library("widget", store.resolve("mylib", url=url, ref="v1"))

    record = catalogue.pin_library("widget", store.resolve("BOSL2", ref="v2"))

    assert [(library.name, library.commit) for library in record.libraries] == [
        ("BOSL2", commits["v2"]),
        ("mylib", commits["v1"]),
    ]


def test_re_pinning_the_same_commit_is_not_a_revision(
    store: LibraryStore, catalogue: Catalogue, history: ModelHistory
) -> None:
    _create(catalogue)
    catalogue.pin_library("widget", store.resolve("BOSL2"))
    head = history.head()

    catalogue.pin_library("widget", store.resolve("BOSL2"))

    assert history.head() == head


def test_pinning_replaces_a_bare_name_from_before_per_model_pins(
    store: LibraryStore, catalogue: Catalogue, paths: DataPaths
) -> None:
    _create(catalogue)
    meta = catalogue.read_raw_meta("widget")
    catalogue.write_raw_meta("widget", {**meta, "libraries": ["BOSL2", "BOSL2"]})

    catalogue.pin_library("widget", store.resolve("BOSL2"))

    stored = json.loads(paths.model_meta("widget").read_text(encoding="utf-8"))
    assert [entry["name"] for entry in stored["libraries"]] == ["BOSL2"]


def test_unpinning_takes_the_library_off_the_model(
    store: LibraryStore, catalogue: Catalogue, paths: DataPaths, history: ModelHistory
) -> None:
    _create(catalogue)
    catalogue.pin_library("widget", store.resolve("BOSL2"))

    record = catalogue.unpin_library("widget", "BOSL2")

    assert record.libraries == []
    assert search_path(paths, declared_libraries(paths.model_dir("widget"))) == ()
    assert history.log(limit=1)[0].message == "Remove library BOSL2 from widget"
    with pytest.raises(LibraryNotDeclaredError):
        catalogue.unpin_library("widget", "BOSL2")


def test_a_listing_is_not_stopped_by_an_entry_it_cannot_read(
    catalogue: Catalogue, paths: DataPaths
) -> None:
    """The listing shows the pins it can read; the render is what refuses (below)."""
    _create(catalogue)
    good = {"name": "ok", "url": "https://x.invalid/b.git", "ref": "v1", "commit": "a" * 40}
    catalogue.write_raw_meta(
        "widget",
        {"name": "Widget", "libraries": ["BOSL2", {"name": "bad", "commit": "HEAD"}, good]},
    )

    assert [library.name for library in catalogue.record("widget").libraries] == ["ok"]


@pytest.mark.parametrize(
    ("libraries", "match"),
    [
        (
            [{"name": "BOSL2", "url": "https://x.invalid/b.git", "ref": "v1", "commit": "HEAD"}],
            "'BOSL2'",
        ),
        (
            [
                {
                    "name": "BOSL2",
                    "url": "https://x.invalid/b.git",
                    "ref": "a..b",
                    "commit": "a" * 40,
                }
            ],
            "ref",
        ),
        ([{"url": "https://x.invalid/b.git", "ref": "v1", "commit": "a" * 40}], "name"),
        ("BOSL2", "not a list"),
    ],
)
def test_a_hand_edited_declaration_is_refused_by_the_render(
    paths: DataPaths, tmp_path: Path, libraries: Any, match: str
) -> None:
    (tmp_path / "model.json").write_text(json.dumps({"libraries": libraries}), encoding="utf-8")

    with pytest.raises(LibraryDeclarationError, match=match):
        declared_libraries(tmp_path)


def test_a_pin_whose_checkout_is_gone_is_an_error(
    store: LibraryStore, paths: DataPaths, upstream: tuple[str, dict[str, str]]
) -> None:
    _, commits = upstream
    pin = store.resolve("BOSL2")
    shutil.rmtree(paths.libraries / "BOSL2" / commits["v1"] / "BOSL2")

    with pytest.raises(LibraryNotInstalledError, match="pin it to this model again at 'v1'"):
        search_path(paths, [pin])


def test_a_bare_name_with_no_lockfile_is_an_error(paths: DataPaths) -> None:
    with pytest.raises(LibraryNotInstalledError, match="BOSL2"):
        search_path(paths, ["BOSL2"])


def test_declared_libraries_reads_model_json(tmp_path: Path) -> None:
    pin = {"name": "dotSCAD", "url": "https://x.invalid/d.git", "ref": "v1", "commit": "a" * 40}
    (tmp_path / "model.json").write_text(
        json.dumps({"name": "x", "libraries": ["BOSL2", pin]}), encoding="utf-8"
    )
    assert declared_libraries(tmp_path) == ["BOSL2", ModelLibrary.model_validate(pin)]
    assert declared_libraries(tmp_path / "missing") == []


async def test_a_render_resolves_the_models_own_pins(
    store: LibraryStore,
    catalogue: Catalogue,
    history: ModelHistory,
    paths: DataPaths,
    upstream: tuple[str, dict[str, str]],
) -> None:
    _, commits = upstream
    _create(catalogue)
    catalogue.pin_library("widget", store.resolve("BOSL2"))

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
    _create(catalogue)
    catalogue.pin_library("widget", store.resolve("BOSL2"))
    written_against = catalogue.version("widget")
    assert written_against is not None
    catalogue.pin_library("widget", store.resolve("BOSL2", ref="v2"))

    old = await resolve_source("widget", written_against, paths=paths, history=history)
    live = await resolve_source("widget", None, paths=paths, history=history)

    assert old.library_path == (paths.libraries / "BOSL2" / commits["v1"],)
    assert live.library_path == (paths.libraries / "BOSL2" / commits["v2"],)


def test_restoring_a_revision_restores_its_pins_and_no_others(
    store: LibraryStore,
    catalogue: Catalogue,
    history: ModelHistory,
    paths: DataPaths,
    upstream: tuple[str, dict[str, str]],
) -> None:
    """#211: a restore moves the restored model's pins, never another model's."""
    _, commits = upstream
    _create(catalogue, "widget")
    _create(catalogue, "gadget")
    catalogue.pin_library("widget", store.resolve("BOSL2"))
    catalogue.pin_library("gadget", store.resolve("BOSL2", ref="v2"))
    written_against = catalogue.version("widget")
    assert written_against is not None
    catalogue.pin_library("widget", store.resolve("BOSL2", ref="v2"))

    history.restore(
        "widget",
        written_against,
        also=lambda commit: pin_restored_declaration(history, paths, "widget", commit),
    )

    assert [lib.commit for lib in catalogue.record("widget").libraries] == [commits["v1"]]
    assert [lib.commit for lib in catalogue.record("gadget").libraries] == [commits["v2"]]


def test_the_orphan_sweep_leaves_libraries_alone(
    store: LibraryStore,
    catalogue: Catalogue,
    paths: DataPaths,
    upstream: tuple[str, dict[str, str]],
) -> None:
    """Checkouts are not slug-keyed: the only model pinning a library being gone
    must not sweep them -- an older revision may still render against it."""
    _, commits = upstream
    _create(catalogue)
    catalogue.pin_library("widget", store.resolve("BOSL2"))
    catalogue.delete("widget")

    catalogue.sweep_orphans()

    assert (paths.libraries / "BOSL2" / commits["v1"] / "BOSL2" / "std.scad").is_file()


# ── the legacy lockfile ───────────────────────────────────────────────────────


def _write_legacy(
    catalogue: Catalogue,
    history: ModelHistory,
    paths: DataPaths,
    lock: dict[str, Any] | str,
    **declared: list[str],
) -> str:
    """A models repository as it was before per-model pins: bare names in each
    model, the pins in one shared lockfile. Returns that revision."""
    for slug, names in declared.items():
        _create(catalogue, slug)
        catalogue.write_raw_meta(slug, {"name": slug.title(), "libraries": names})
    body = lock if isinstance(lock, str) else json.dumps(lock, indent=2)
    (paths.models / LOCKFILE_NAME).write_text(body, encoding="utf-8")
    commit = history.commit("legacy", LOCKFILE_NAME, *declared)
    assert commit is not None
    return commit


def _pin(store: LibraryStore, ref: str = "v1") -> dict[str, str]:
    """A fetched pin as the legacy lockfile wrote it: no name."""
    return store.resolve("BOSL2", ref=ref).model_dump(exclude={"name"})


def test_the_migration_moves_each_pin_into_the_models_that_declare_it(
    store: LibraryStore, catalogue: Catalogue, history: ModelHistory, paths: DataPaths
) -> None:
    pin = _pin(store)
    _write_legacy(catalogue, history, paths, {"BOSL2": pin}, widget=["BOSL2"], gadget=[])

    migrated = migrate_lockfile(paths, history, ["widget", "gadget"])

    assert migrated == ["widget"]
    stored = json.loads(paths.model_meta("widget").read_text(encoding="utf-8"))
    assert stored["libraries"] == [{"name": "BOSL2", **pin}]
    assert not (paths.models / LOCKFILE_NAME).exists()
    latest = history.log(limit=1)[0]
    assert sorted(change.path for change in latest.files) == [LOCKFILE_NAME, "widget/model.json"]
    # Nothing left to do on the next boot.
    assert migrate_lockfile(paths, history, ["widget", "gadget"]) == []


def test_the_migration_leaves_a_name_the_lock_cannot_pin(
    store: LibraryStore, catalogue: Catalogue, history: ModelHistory, paths: DataPaths
) -> None:
    pin = _pin(store)
    broken = {**pin, "commit": "HEAD"}
    _write_legacy(
        catalogue, history, paths, {"BOSL2": pin, "broken": broken}, widget=["BOSL2", "broken"]
    )

    migrate_lockfile(paths, history, ["widget"])

    declared = declared_libraries(paths.model_dir("widget"))
    assert declared == [ModelLibrary(name="BOSL2", **pin), "broken"]
    with pytest.raises(LibraryNotInstalledError, match="'broken'"):
        search_path(paths, declared)


def test_an_unreadable_lockfile_is_not_migrated_or_removed(
    catalogue: Catalogue, history: ModelHistory, paths: DataPaths
) -> None:
    _write_legacy(catalogue, history, paths, "{not json", widget=["BOSL2"])

    assert migrate_lockfile(paths, history, ["widget"]) == []

    assert (paths.models / LOCKFILE_NAME).read_text(encoding="utf-8") == "{not json"
    lock = read_lock(paths)
    assert lock is not None
    with pytest.raises(LibraryDeclarationError, match="not valid JSON"):
        search_path(paths, ["BOSL2"], lock)


async def test_a_revision_from_before_the_migration_renders_against_its_lockfile(
    store: LibraryStore,
    catalogue: Catalogue,
    history: ModelHistory,
    paths: DataPaths,
    upstream: tuple[str, dict[str, str]],
) -> None:
    _, commits = upstream
    legacy = _write_legacy(catalogue, history, paths, {"BOSL2": _pin(store)}, widget=["BOSL2"])
    migrate_lockfile(paths, history, ["widget"])
    catalogue.pin_library("widget", store.resolve("BOSL2", ref="v2"))

    old = await resolve_source("widget", legacy, paths=paths, history=history)

    assert old.library_path == (paths.libraries / "BOSL2" / commits["v1"],)


def test_restoring_a_revision_from_before_the_migration_pins_it_in_the_model(
    store: LibraryStore,
    catalogue: Catalogue,
    history: ModelHistory,
    paths: DataPaths,
    upstream: tuple[str, dict[str, str]],
) -> None:
    _, commits = upstream
    legacy = _write_legacy(catalogue, history, paths, {"BOSL2": _pin(store)}, widget=["BOSL2"])
    migrate_lockfile(paths, history, ["widget"])
    catalogue.pin_library("widget", store.resolve("BOSL2", ref="v2"))

    history.restore(
        "widget",
        legacy,
        also=lambda commit: pin_restored_declaration(history, paths, "widget", commit),
    )

    assert [lib.commit for lib in catalogue.record("widget").libraries] == [commits["v1"]]
    # The lockfile stays gone: the restore is the model's alone.
    assert not (paths.models / LOCKFILE_NAME).exists()


def test_lock_at_reads_the_lock_as_it_was_at_a_revision(
    store: LibraryStore, catalogue: Catalogue, history: ModelHistory, paths: DataPaths
) -> None:
    before_any = history.head()
    assert before_any is not None
    pin = _pin(store)
    legacy = _write_legacy(catalogue, history, paths, {"BOSL2": pin}, widget=["BOSL2"])

    assert lock_at(history, before_any) == Lock()
    assert lock_at(history, legacy).pins["BOSL2"] == LibraryPin.model_validate(pin)


def _builtin(paths: DataPaths, slug: str, libraries: list[Any]) -> str:
    """A built-in's mirror, as the boot sync writes it from the image."""
    builtin = f"builtin:{slug}"
    paths.model_dir(builtin).mkdir(parents=True)
    paths.model_source(builtin).write_text(SOURCE, encoding="utf-8")
    meta = {"name": slug.title(), "libraries": libraries}
    paths.model_meta(builtin).write_text(json.dumps(meta), encoding="utf-8")
    return builtin


def test_the_migration_never_writes_a_built_in_and_keeps_the_lock_it_needs(
    store: LibraryStore,
    catalogue: Catalogue,
    history: ModelHistory,
    paths: DataPaths,
    upstream: tuple[str, dict[str, str]],
) -> None:
    """A built-in's model.json is the image's: the boot sync would put a rewrite
    back, with the lockfile gone. So it is left as mirrored, and the lockfile stays
    for its renders to read."""
    _, commits = upstream
    pin = _pin(store)
    _write_legacy(catalogue, history, paths, {"BOSL2": pin}, widget=["BOSL2"])
    builtin = _builtin(paths, "kit", ["BOSL2"])
    mirrored = paths.model_meta(builtin).read_text(encoding="utf-8")

    migrated = migrate_lockfile(paths, history, ["widget", builtin])

    assert migrated == ["widget"]
    assert paths.model_meta(builtin).read_text(encoding="utf-8") == mirrored
    assert (paths.models / LOCKFILE_NAME).is_file()
    assert model_search_path(paths, builtin) == (paths.libraries / "BOSL2" / commits["v1"],)
    # Its own model is migrated all the same, and a second boot changes nothing.
    assert declared_libraries(paths.model_dir("widget")) == [ModelLibrary(name="BOSL2", **pin)]
    assert migrate_lockfile(paths, history, ["widget", builtin]) == []


def test_the_lock_goes_once_no_built_in_declares_by_name(
    store: LibraryStore, catalogue: Catalogue, history: ModelHistory, paths: DataPaths
) -> None:
    pin = _pin(store)
    _write_legacy(catalogue, history, paths, {"BOSL2": pin}, widget=["BOSL2"])
    builtin = _builtin(paths, "kit", [{"name": "BOSL2", **pin}])

    migrate_lockfile(paths, history, ["widget", builtin])

    assert not (paths.models / LOCKFILE_NAME).exists()
